import io
import re
import json
import time
import struct
import logging
import asyncio
import warnings
import alsaaudio
import statistics
from typing import Optional
from itertools import islice
from collections import namedtuple
from dataclasses import dataclass

from mpris import MPRISConnector
from config import *

@dataclass
class AlsaDeviceConfig:
    pcm_name: str
    format: str
    size: int
    signed: bool
    endian: str
    channels: int
    rate: int
    period_size: int
    period_time: float
    maxamp: int
    reference: int

    is_signed = {'S': True, 'U': False}
    fmt_matcher = re.compile(r'.*(S|U)(\d+)_?(LE|BE)?$').match

    def __init__(self,
                 pcm_name: str = 'default',
                 pcm_data_format: str ='PCM_FORMAT_S16_LE',
                 channels: int = 2,
                 rate: int = 48000,
                 period_frames: int = 1024):
        res = self.fmt_matcher(pcm_data_format)
        
        if res:
            sign, bits, endian = res.groups()
            self.size = int(bits) // 8
            self.signed = self.is_signed[sign]
            self.endian = endian
        else:
            warnings.warn('Invalid format string. Falling back to \'PCM_FORMAT_S16_LE\'')
            pcm_data_format = 'PCM_FORMAT_S16_LE'
            self.size, self.signed, self.endian = 2, True, 'LE'

        self.pcm_name = pcm_name
        self.format = pcm_data_format
        self.channels = channels
        self.rate = rate
        self.frame_size = channels * self.size
        self.period_size = period_frames
        self.period_time = period_frames / rate
        self.maxamp = 1 << int(bits) - 1
        self.reference = 0 if self.signed else self.maxamp


@dataclass
class AlsaDevice:
    name: str
    cfg: AlsaDeviceConfig
    dev_type: int
    dev_mode: int = alsaaudio.PCM_NONBLOCK
    index: Optional[int] = None
    occupied: bool = False
    device: alsaaudio.PCM = None

    def __init__(self, cfg: AlsaDeviceConfig = AlsaDeviceConfig()):
        self.name = self._pick(cfg.pcm_name)
        self.cfg = cfg

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        self.device = alsaaudio.PCM(self.dev_type, self.dev_mode, device=self.name)
        self.device.setchannels(self.cfg.channels)
        self.device.setrate(self.cfg.rate)
        self.device.setformat(getattr(alsaaudio, self.cfg.format))
        self.device.setperiodsize(self.cfg.period_size)

    def close(self):
        self.device.close()

    def _pick(self, name):
        pcms = alsaaudio.pcms(self.dev_type)
        if name == 'default' or name in pcms:
            return name

        try:
            ret = [n for n in pcms if 'sysdefault:CARD' in n][0]
        except IndexError:
            ret = 'default'
        return  ret


class CaptureDevice(AlsaDevice):
    dev_type: int = alsaaudio.PCM_CAPTURE

    def read(self):
        while True:
            length, data = self.device.read()
            if length > 0:
                return data
            elif length == 0:
                warnings.warn(f'Incomplete read {length=}', RuntimeWarning)
            elif length == -32:
                warnings.warn(f'Broken Pipe: {length}', RuntimeWarning)
            else:
                warnings.warn(f'Unknown Error: Code={length}', RuntimeWarning)
            time.sleep(0.010)


class PlaybackDevice(AlsaDevice):
    dev_type: int = alsaaudio.PCM_PLAYBACK

    def write(self, data):
        while True:
            written = self.device.write(data)

            if written == 0:
                logging.warning('Write buffer full on playback')
                continue

            return written


class MemScope:
    endian_t = {
        None: '' ,
        'LE': '<',
        'BE': '>',
    }
    struct_t = {
    #   (signed, size)
        (  True,    1): 'b',
        ( False,    1): 'c',
        (  True,    2): 'h',
        ( False,    2): 'H',
        (  True,    3): 'i',
        ( False,    3): 'I',
        (  True,    4): 'i',
        ( False,    4): 'I',
    }
    padding_t = {
    #   (signed, size, endian)
        (  True,    3,   'LE'): lambda x: x + (b'\0' if x[2] < 128 else b'\xff'),
        ( False,    3,   'LE'): lambda x: x + b'\0',
        (  True,    3,   'BE'): lambda x: (b'\0' if x[2] < 128 else b'\xff') + x,
        ( False,    3,   'BE'): lambda x: b'\xff' + x,
    }

    def __init__(self,
                 data: bytearray,
                 dev_cfg: AlsaDeviceConfig):
        self.dev_cfg = dev_cfg
        self.buffer = io.BytesIO(data)
        self.struct = struct.Struct(self._struct_str)

        pad_lookup = (dev_cfg.signed, dev_cfg.size, dev_cfg.endian)
        self.padding = self.padding_t.get(pad_lookup, lambda x: x)

    def __iter__(self):
        self.buffer.seek(0)
        return self

    def __next__(self):
        chunk = b''
        try:
            for _ in range(self.dev_cfg.channels):
                chunk += self.padding(self.buffer.read(self.dev_cfg.size))
            return self.struct.unpack(chunk)
        except (struct.error, IndexError):
            raise StopIteration

    @property
    def _struct_str(self):
        endian = self.endian_t[self.dev_cfg.endian]
        body = self.struct_t[(self.dev_cfg.signed, self.dev_cfg.size)] \
               * self.dev_cfg.channels
        return (f'{endian}{body}')


class HystComp:
    def __init__(self, low: float, high: float):
        logging.debug('hysteresis comparator registered as (%.1f, %.1f)', low, high)
        self.low = low
        self.high = high

    def __repr__(self):
        return f'<HystComp({self.low:.2e}, {self.high:.2e}>'

    def comp(self, val):
        CompResult = namedtuple('HystCompResult', ['low', 'high'])
        return CompResult(val <= self.low, val >= self.high)


class LoopStateMachine:
    def __init__(self,
                 capture_cfg: AlsaDeviceConfig,
                 playback_cfg: AlsaDeviceConfig):
        self.rxq = asyncio.Queue()
        self.capture_cfg = capture_cfg
        self.playback_cfg = playback_cfg
        self.probe_cfg = self.__load_config()
        self.capture = None
        self.dbus = None
        self.probe = self.probe_cfg.sensitivity

        self._local_state = PlayerState.UNKNOWN
        self._buffer = b''

    def __reverse_db(self, db):
        return self.capture_cfg.maxamp * 10**(db/20)

    @property
    def probe(self):
        """Calculated median from data across all channels.
           Takes around 1 ms at 40 frames with setero data on pi3. Slow but acceptable.
        """
        samples = islice(MemScope(self._buffer, self.capture_cfg), self.probe_cfg.sample_size)
        data = [abs(val - self.capture_cfg.reference) for packet in samples for val in packet]
        med = statistics.median(data)
        logging.debug('polled med %.0f <> threshold (%.0f, %.0f)',
                      med, self._threscomp.low, self._threscomp.high)
        return self._threscomp.comp(med)

    @probe.setter
    def probe(self, val):
        if val == 0:
            self._threscomp = HystComp(0, 0)
        else:
            val = -abs(val)
            self._threscomp = HystComp(
                self.__reverse_db(val),
                self.__reverse_db(val - 3))

    def __load_config(self):
        default = ProbeConfig()
        try:
            with open(Env.CFGFILE, 'r') as fp:
                json_db = json.load(fp)
                default.update(json_db)
                logging.info('Load config from %s', Env.CFGFILE)
        except (FileNotFoundError, json.decoder.JSONDecodeError):
            logging.info('Cannot read %s. Using default configuration', Env.CFGFILE)
        return default

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def open(self):
        self.capture = CaptureDevice(self.capture_cfg)
        self.dbus = MPRISConnector(self.rxq)
        self.capture.open()
        self.dbus.open()

    def close(self):
        self.capture.close()
        self.dbus.close()

    @property
    def state(self):
        return self._local_state

    @state.setter
    def state(self, val):
        self._local_state = val
        try:
            self.dbus.player.PlaybackStatus = MPRISStatus[val]
        except AttributeError:
            logging.warning('Cannot connect to DBus.')

    async def run(self):
        TaskInfo = namedtuple('TaskInfo', ['state', 'delay', 'coro'])
        manifests = {
            PlayerCommand.STOP:  TaskInfo(PlayerState.HYBERNATE,
                                          self.probe_cfg.hybernate_interval,
                                          self._wake),
            PlayerCommand.PLAY:  TaskInfo(PlayerState.IDLE,
                                          self.probe_cfg.idle_interval,
                                          self._idle),
        }

        self.loop = asyncio.get_running_loop()
        self.dbus.aioloop = self.loop
        await self.rxq.put(PlayerCommand.PLAY)

        while self.state is not PlayerState.KILLED:
            cmd = await self.rxq.get()
            await self._gather()

            todo = manifests[cmd]
            logging.info('Received command %s. Dispatching %.1f seconds', cmd, todo.delay)
            self.state = todo.state
            self.loop.call_later(todo.delay, asyncio.create_task, todo.coro())
            self.rxq.task_done()

    async def _wake(self):
        self.state = PlayerState.IDLE
        self.loop.create_task(self._idle())

    async def _idle(self):
        counter = 0
        while self.state == PlayerState.IDLE:
            self._buffer = self.capture.read()
            if self.probe.high:
                counter += 1
            else:
                counter = 0

            if counter >= self.probe_cfg.start_count:
                self.state = PlayerState.PLAY
                self.loop.create_task(self._stream())
                await asyncio.sleep(self.probe_cfg.stream_interval)
                self.loop.create_task(self._monitor())
            elif counter > 0:
                await asyncio.sleep(self.probe_cfg.follow_interval)
            else:
                await asyncio.sleep(self.probe_cfg.idle_interval)

    async def _monitor(self):
        counter = 0
        while self.state == PlayerState.PLAY:
            if self.probe.low:
                counter += 1
            else:
                counter = 0

            if counter >= self.probe_cfg.stop_count:
                self.state = PlayerState.IDLE
                await asyncio.sleep(self.probe_cfg.idle_interval)
                self.loop.create_task(self._idle())
            elif counter > 0:
                await asyncio.sleep(self.probe_cfg.follow_interval)
            else:
                await asyncio.sleep(self.probe_cfg.stream_interval)

    async def _stream(self):
        logging.info('start redirecting [%s] => [%s]', self.capture.name, self.playback_cfg.pcm_name)

        try:
            with PlaybackDevice(self.playback_cfg) as playback:
                playback.write(self._buffer)

                while self.state == PlayerState.PLAY:
                    self._buffer = self.capture.read()
                    playback.write(self._buffer)
                    await asyncio.sleep(0.001)
        except alsaaudio.ALSAAudioError as e:
            logging.info('Error opening playback device: %s', e)
            await self.rxq.put(PlayerCommand.STOP)

        logging.info('close playback')

    async def _gather(self):
        tasks = [t for t in asyncio.all_tasks() if t is not
                 asyncio.current_task()]

        [task.cancel() for task in tasks]

        logging.info('Cancelling %d outstanding tasks', len(tasks))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _restart(self, sig):
        self.state = PlayerState.UNKNOWN
        logging.info('Received restart signal %s.', sig.name)
        await self._gather()
        self.probe_cfg = self.__load_config()
        self.loop.create_task(self.run())

    async def _shutdown(self, sig):
        self.state = PlayerState.KILLED
        logging.info('Received exit signal %s.', sig.name)
        await self._gather()
        self.close()
        self.loop.stop()

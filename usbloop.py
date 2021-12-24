#!/usr/bin/env python

import io
import os
import re
import sys
import struct
import logging
import asyncio
import warnings
import alsaaudio
import itertools
from struct import unpack_from
from typing import Union, Iterable, Callable
from collections import namedtuple
from dataclasses import dataclass

logging.captureWarnings(True)
logging.basicConfig(level=logging.DEBUG)

@dataclass
class StreamMeta:
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

    def __init__(self,
                 pcm_data_format: str ='PCM_FORMAT_S16_LE',
                 channels: int = 2,
                 rate: int = 48000,
                 period_frames: int = 1024):
        is_signed = {'S': True, 'U': False}
        res = re.match(r'.*(S|U)(\d+)_?(LE|BE)?$', pcm_data_format)
        sign, bits, endian = res.groups()
        self.size = int(bits) // 8
        self.signed = is_signed[sign]
        self.endian = endian
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
    meta: StreamMeta
    dev_type: int
    dev_mode: int = alsaaudio.PCM_NONBLOCK
    index: Union[int, None] = None
    occupied: bool = False
    device: alsaaudio.PCM = None
    wait_time: float = 0.001

    def __init__(self,
                 pcm_name: str,
                 stream_meta: StreamMeta = StreamMeta()):
        self.name = pcm_name
        self.meta = stream_meta

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return True

    def open(self):
        self.device = alsaaudio.PCM(self.dev_type, self.dev_mode, device=self.name)
        self.device.setchannels(self.meta.channels)
        self.device.setrate(self.meta.rate)
        self.device.setformat(getattr(alsaaudio, self.meta.format))
        self.device.setperiodsize(self.meta.period_size)

    def close(self):
        self.device.close()


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


class PlaybackDevice(AlsaDevice):
    dev_type: int = alsaaudio.PCM_PLAYBACK

    def write(self, data):
        while True:
            written = self.device.write(data)

            if written == 0:
                warnings.warn(f'Buffer full', RuntimeWarning)
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
                 meta: StreamMeta):
        self.meta = meta
        self.buffer = io.BytesIO(data)
        self.struct = struct.Struct(self._struct_str)
        self.padding = self.padding_t.get(
                (meta.signed, meta.size, meta.endian), lambda x: x)

    def __iter__(self):
        self.buffer.seek(0)
        return self

    def __next__(self):
        chunk = b''
        try:
            for c in range(self.meta.channels):
                chunk += self.padding(self.buffer.read(self.meta.size))
            return self.struct.unpack(chunk)
        except (struct.error, IndexError):
            raise StopIteration

    @property
    def _struct_str(self):
        return (f'{self.endian_t[self.meta.endian]}'
                f'{self.struct_t[(self.meta.signed, self.meta.size)] * self.meta.channels}')


@dataclass
class ProbeConfig:
    idle_interval: float = .25
    streaming_interval: float = 1.0
    start_count: int = 3
    stop_count: int = 5


class FSM:
    Thresholds = namedtuple('Thresholds', ['start', 'stop'])

    def __init__(self,
                 capture_dev: CaptureDevice,
                 probing: ProbeConfig = ProbeConfig(),
                 playback_pcm_name: str = 'default',
                 threshold_db: float = -10):
        self.loop = None
        self.counter = 0
        self.is_streaming = False
        self.probing = probing
        self.meta = capture_dev.meta
        self.capture = capture_dev
        self.playback_pcm_name = playback_pcm_name
        self.buffer = b''
        self.thresholds = self.Thresholds(
                self.__reverse_db(threshold_db),
                self.__reverse_db(threshold_db - 3)
        )

    def __reverse_db(self, db):
        return self.meta.maxamp * 10**(-abs(db)/20)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.loop.call_later(self.probing.idle_interval, asyncio.create_task, self.probe_idle())

    async def probe_idle(self):
        self.buffer = await self.capture.read()
        logging.debug(f'read {len(self.buffer)} bytes from device {self.capture.name}')

        for packet in MemScope(self.buffer, self.meta):
            if any(abs(p) > self.thresholds.start for p in packet):
                self.counter += 1
                logging.debug(f'probe_idle: {packet} > {self.thresholds.start:.0f}, {self.counter=}')
                break
        else:
            self.counter = 0
            logging.debug(f'probe_idle: no audio detected, {self.counter=}')

        if self.counter > self.probing.start_count:
            self.counter = 0
            self.is_streaming = True
            self.loop.create_task(self.stream())
            self.loop.call_later(self.probing.streaming_interval, asyncio.create_task, self.probe_stream())

        else:
            self.loop.call_later(self.probing.idle_interval, asyncio.create_task, self.probe_idle())

    async def probe_stream(self):
        for packet in MemScope(self.buffer, self.meta):
            if all(abs(p) < self.thresholds.stop for p in packet):
                self.counter += 1
                logging.debug(f'probe_stream: {packet} < {self.thresholds.stop:.0f}, {self.counter=}')
                break
        else:
            self.counter = 0

        if self.counter > self.probing.stop_count:
            self.is_streaming = False
            self.counter = 0
            self.loop.call_later(self.probing.idle_interval, asyncio.create_task, self.probe_idle())

        else:
            self.loop.call_later(self.probing.streaming_interval, asyncio.create_task, self.probe_stream())

    async def stream(self):
        logging.info(f'start looping from device {self.capture.name}')

        with PlaybackDevice(self.playback_pcm_name, self.meta) as playback:
            await playback.write(self.buffer)

            while self.is_streaming:
                self.buffer = await self.capture.read()
                await asyncio.sleep(self.meta.period_time * 0.75)
                #await playback.write(self.buffer)

        logging.info('close playback')


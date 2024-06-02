# -*- coding: utf-8 -*-
#
# GPL License and Copyright Notice ============================================
#  This file is part of Wrye Bash.
#
#  Wrye Bash is free software: you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation, either version 3
#  of the License, or (at your option) any later version.
#
#  Wrye Bash is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Wrye Bash.  If not, see <https://www.gnu.org/licenses/>.
#
#  Wrye Bash copyright (C) 2005-2009 Wrye, 2010-2024 Wrye Bash Team
#  https://github.com/wrye-bash
#
# =============================================================================

"""Save files - beta - TODOs:
- that's the headers code only - write save classes (per game)
- rework encoding/decoding
"""
from __future__ import annotations

__author__ = u'Utumno'

import copy
import io
import os
import sys
import zlib
from enum import Enum
from functools import partial
from itertools import repeat

import lz4.block

from .. import bolt
from ..bolt import FName, cstrip, decoder, deprint, encode, pack_byte, \
    pack_bzstr8, pack_float, pack_int, pack_short, pack_str8, \
    remove_newlines, struct_error, struct_unpack, structs_cache, unpack_byte, \
    unpack_float, unpack_int, unpack_many, unpack_short, unpack_str8, \
    unpack_str16, unpack_str16_delim, unpack_str_byte_delim, \
    unpack_str_int_delim, gen_enum_parser, unpack_int64
from ..exception import SaveHeaderError

# Utilities -------------------------------------------------------------------
def _unpack_fstr16(ins) -> bytes:
    return ins.read(16)
def _pack_c(out, value, __pack=structs_cache['=c'].pack):
    out.write(__pack(value))
def _pack_string(out, val: bytes):
    out.write(val)
def _skip_str8(ins):
    ins.seeek(unpack_byte(ins), 1)
def _skip_str16(ins):
    ins.seek(unpack_short(ins), 1)
def _write_s16_list(out, master_bstrs):
    for master_bstr in master_bstrs:
        pack_short(out, len(master_bstr))
        out.write(master_bstr)
def _pack_str8_1(out, val): # TODO: val = val.reencode(...)
    val = encode(val)
    pack_bzstr8(out, val)
    return len(val) + 2

##: Maybe all this (de)compression stuff should go to bolt? Then we could try
# deduplicating the BSA ones as well
class _SaveCompressionType(Enum):
    """The possible types of compression that saves can have. Not all games
    have all of these available."""
    NONE = 0
    ZLIB = 1
    LZ4 = 2

_sc_parser = gen_enum_parser(_SaveCompressionType)

def _decompress_save(ins, compressed_size: int, decompressed_size: int,
        compression_type: _SaveCompressionType, *, light_decompression=False):
    """Decompress the specified data using either LZ4 or zlib, depending on
    compression_type. Do not call for uncompressed files!"""
    match compression_type:
        case _SaveCompressionType.ZLIB:
            decompressor = _decompress_save_zlib
        case _SaveCompressionType.LZ4:
            if light_decompression:
                decompressor = _decompress_save_lz4_light
            else:
                decompressor = _decompress_save_lz4
        case _:
            raise SaveHeaderError(f'Unknown compression type '
                                  f'{compression_type} or uncompressed file')
    return decompressor(ins, compressed_size, decompressed_size)

def _decompress_save_zlib(ins, compressed_size: int, decompressed_size: int):
    """Decompress compressed_size bytes from the specified input stream using
    zlib, sanity-checking decompressed size afterwards."""
    try:
        decompressed_data = zlib.decompress(ins.read(compressed_size))
    except zlib.error as e:
        raise SaveHeaderError(f'zlib error while decompressing '
                              f'zlib-compressed header: {e!r}')
    if len(decompressed_data) != decompressed_size:
        raise SaveHeaderError(
            f'zlib-decompressed header size incorrect - expected '
            f'{decompressed_size}, but got {len(decompressed_data)}.')
    return io.BytesIO(decompressed_data)

def _decompress_save_lz4(ins, compressed_size: int, decompressed_size: int):
    """Decompress compressed_size bytes from the specified input stream using
    LZ4, sanity-checking decompressed size afterwards."""
    try:
        decompressed_data = lz4.block.decompress(
            ins.read(compressed_size), uncompressed_size=decompressed_size * 2)
    except lz4.block.LZ4BlockError as e:
        raise SaveHeaderError(f'LZ4 error while decompressing '
                              f'lz4-compressed header: {e!r}')
    if (len_data := len(decompressed_data)) != decompressed_size:
        raise SaveHeaderError(f'lz4-decompressed header size incorrect - '
            f'expected {decompressed_size}, but got {len_data}.')
    return io.BytesIO(decompressed_data)

def _decompress_save_lz4_light(ins, _comp_size: int, _decomp_size: int):
    """Read the start of the LZ4 compressed data in the SSE savefile and
    stop when the whole master table is found.
    Return a file-like object that can be read by _load_masters_16
    containing the now decompressed master table.
    See https://fastcompression.blogspot.se/2011/05/lz4-explained.html
    for an LZ4 explanation/specification."""
    def _read_lsic_int():
        """Read a compressed int from the stream.
        In short, add every byte to the output until a byte lower than
        255 is found, then add that as well and return the total sum.
        LSIC stands for linear small-integer code, taken from
        https://ticki.github.io/blog/how-lz4-works."""
        result = 0
        while True:  # there is no size limit to LSIC values
            num = unpack_byte(ins)
            result += num
            if num != 255:
                return result
    uncompressed = b''
    masters_size: int | None = None
    while True:  # parse and decompress each block here
        token = unpack_byte(ins)
        # How many bytes long is the literals-field?
        literal_length = token >> 4
        if literal_length == 15:  # add more if we hit max value
            literal_length += _read_lsic_int()
        # Read all the literals (which are good ol' uncompressed bytes)
        uncompressed += ins.read(literal_length)
        # The offset is how many bytes back in the uncompressed string the
        # start of the match-field (copied bytes) is
        offset = unpack_short(ins)
        # How many bytes long is the match-field?
        match_length = token & 0b1111
        if match_length == 15:
            match_length += _read_lsic_int()
        match_length += 4  # the match-field always gets an extra 4 bytes
        # The boundary of the match-field
        start_pos = len(uncompressed) - offset
        end_pos = start_pos + match_length
        # Matches can be overlapping (aka including not yet decompressed
        # data) so we can't jump the whole match_length directly
        while start_pos < end_pos:
            uncompressed += uncompressed[start_pos:min(start_pos + offset,
                                                       end_pos)]
            start_pos += offset
        # The masters table's size is found in bytes 1-5
        if masters_size is None and len(uncompressed) >= 5:
            masters_size = struct_unpack('I', uncompressed[1:5])[0]
        # Stop when we have the whole masters table
        if masters_size is not None:
            if len(uncompressed) >= masters_size + 5:
                break
    # Wrap the decompressed data in a file-like object and return it
    return io.BytesIO(uncompressed)

def _compress_save(to_compress: io.BytesIO,
        compression_type: _SaveCompressionType):
    """Compress the specified data using either LZ4 or zlib, depending on
    compression_type. Do not call for uncompressed files!"""
    try:
        match compression_type:
            case _SaveCompressionType.ZLIB:
                # SSE uses zlib level 1
                # TODO(SF) What does Starfield use?
                return zlib.compress(to_compress.getvalue(), 1)
            case _SaveCompressionType.LZ4:
                # SSE uses default lz4 settings; store_size is not in docs, so:
                # noinspection PyArgumentList
                return lz4.block.compress(to_compress.getvalue(),
                                          store_size=False)
            case _:
                raise SaveHeaderError(f'Unknown compression type '
                                      f'{compression_type} or uncompressed '
                                      f'file')
    except (zlib.error, lz4.block.LZ4BlockError) as e:
        raise SaveHeaderError(f'Failed to compress header: {e!r}')

def calc_time_fo4(gameDate: bytes) -> (float, int):
    """Handle time calculation from FO4 and newer games. Takes gameDate and
    returns gameDays and gameTicks."""
    # gameDate format: Xd.Xh.Xm.X days.X hours.X minutes
    # russian game format: '0д.0ч.9м.0 д.0 ч.9 мин'
    # So handle it by concatenating digits until we hit a non-digit char
    def parse_int(gd_bytes: bytes):
        int_data = b''
        for i in gd_bytes:
            c = i.to_bytes(1, sys.byteorder)
            if c.isdigit():
                int_data += c
            else:
                break # hit the end of the int
        return int(int_data)
    days, hours, minutes = [parse_int(x) for x in
                            gameDate.split(b'.')[:3]]
    gameDays = float(days) + float(hours) / 24 + float(minutes) / (24 * 60)
    # Assuming still 1000 ticks per second
    gameTicks = (days * 24 * 60 * 60 + hours * 60 * 60 + minutes * 60) * 1000
    return gameDays, gameTicks

# Save Headers ----------------------------------------------------------------
class SaveFileHeader(object):
    save_magic: bytes
    # common slots Bash code expects from SaveHeader (added header_size and
    # turned image to a property)
    __slots__ = (u'header_size', u'pcName', u'pcLevel', u'pcLocation',
                 u'gameDays', u'gameTicks', u'ssWidth', u'ssHeight', u'ssData',
                 u'masters', u'_save_info', u'_mastersStart')
    # map slots to (seek position, unpacker) - seek position negative means
    # seek relative to ins.tell(), otherwise to the beginning of the file
    _unpackers = {}
    # Same as _unpackers, but processed immediately after the screenshot is
    # read
    _unpackers_post_ss = {}

    def __init__(self, save_inf, load_image=False, ins=None):
        self._save_info = save_inf
        self.ssData = None # lazily loaded at runtime
        self.read_save_header(load_image, ins)

    def read_save_header(self, load_image=False, ins=None):
        """Fully reads this save header, optionally loading the image as
        well."""
        try:
            if ins is None:
                with self._save_info.abs_path.open(u'rb') as ins:
                    self.load_header(ins, load_image)
            else:
                self.load_header(ins, load_image)
        #--Errors
        except (OSError, struct_error) as e:
            err_msg = f'Failed to read {self._save_info.abs_path}'
            deprint(err_msg, traceback=True)
            raise SaveHeaderError(err_msg) from e

    def _load_from_unpackers(self, ins, target_unpackers):
        for attr, (__pack, _unpack) in target_unpackers.items():
            setattr(self, attr, _unpack(ins))

    def load_header(self, ins, load_image=False):
        save_magic = ins.read(len(self.__class__.save_magic))
        if save_magic != self.__class__.save_magic:
            raise SaveHeaderError(f'Magic wrong: {save_magic!r} (expected '
                                  f'{self.__class__.save_magic!r})')
        self._load_from_unpackers(ins, self.__class__._unpackers)
        self.load_image_data(ins, load_image)
        self._load_masters(ins)
        # additional calculations - TODO(ut): rework decoding
        self.calc_time()
        self.pcName = remove_newlines(decoder(cstrip(self.pcName)))
        self.pcLocation = remove_newlines(decoder(
            cstrip(self.pcLocation), bolt.pluginEncoding,
            avoidEncodings=(u'utf8', u'utf-8')))
        self._decode_masters()

    def dump_header(self, out):
        raise NotImplementedError

    def load_image_data(self, ins, load_image=False):
        bpp = (4 if self.has_alpha else 3)
        image_size = bpp * self.ssWidth * self.ssHeight
        if load_image:
            self.ssData = bytearray(ins.read(image_size))
        else:
            ins.seek(image_size, 1)

    def _load_masters(self, ins):
        self._load_from_unpackers(ins, self.__class__._unpackers_post_ss)
        self._mastersStart = ins.tell()
        self.masters = []
        numMasters = unpack_byte(ins)
        append_master = self.masters.append
        for count in range(numMasters):
            append_master(unpack_str8(ins))

    def _decode_masters(self):
        self.masters = [FName(decoder(x, bolt.pluginEncoding,
            avoidEncodings=(u'utf8', u'utf-8'))) for x in self.masters]

    def _encode_masters(self):
        self.masters = [encode(x) for x in self.masters] ##: encoding?

    def remap_masters(self, master_map):
        """Remaps all masters in this save header according to the specified
        master map (dict mapping old -> new FNames)."""
        self.masters = [master_map.get(x, x) for x in self.masters]

    def calc_time(self): pass

    @property
    def has_alpha(self):
        """Whether or not this save file has alpha."""
        return False

    @property
    def image_loaded(self):
        """Whether or not this save header has had its image loaded yet."""
        return self.ssData is not None

    @property
    def image_parameters(self):
        return self.ssWidth, self.ssHeight, self.ssData, self.has_alpha

    def write_header(self, ins, out):
        """Write out the full save header. Needs the unaltered file stream as
        input."""
        # Work with encoded masters for the entire writing process
        self._encode_masters()
        self._do_write_header(ins, out)
        self._decode_masters()

    def _do_write_header(self, ins, out):
        out.write(ins.read(self._mastersStart))
        self._write_masters(ins, out)
        #--Copy the rest
        for block in iter(partial(ins.read, 0x5000000), b''):
            out.write(block)

    def _write_masters(self, ins, out):
        ins.seek(4, 1) # Discard oldSize
        pack_int(out, self._master_block_size())
        ins.seek(1, 1) # Skip old master count
        self._dump_masters(ins, out)
        #--Offsets
        offset = out.tell() - ins.tell()
        #--File Location Table
        for i in range(6):
            # formIdArrayCount offset, unkownTable3Offset,
            # globalDataTable1Offset, globalDataTable2Offset,
            # changeFormsOffset, globalDataTable3Offset
            oldOffset = unpack_int(ins)
            pack_int(out, oldOffset + offset)

    def _dump_masters(self, ins, out):
        for _x in range(len(self.masters)):
            _skip_str16(ins)
        #--Write new masters
        pack_byte(out, len(self.masters))
        _write_s16_list(out, self.masters)

    def _master_block_size(self):
        return 1 + sum(len(x) + 2 for x in self.masters)

    @property
    def can_edit_header(self):
        """Whether or not this header can be edited - if False, it will still
        be read and displayed, but the Save/Cancel buttons will be disabled."""
        return True

class OblivionSaveHeader(SaveFileHeader):
    save_magic = b'TES4SAVEGAME'
    __slots__ = (u'major_version', u'minor_version', u'exe_time',
                 u'header_version', u'saveNum', u'gameTime', u'ssSize')

    ##: exe_time and gameTime are SYSTEMTIME structs, as described here:
    # https://docs.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-systemtime
    _unpackers = {
        'major_version':  (pack_byte, unpack_byte),
        'minor_version':  (pack_byte, unpack_byte),
        'exe_time':       (_pack_string, _unpack_fstr16),
        'header_version': (pack_int, unpack_int),
        'header_size':    (pack_int, unpack_int),
        'saveNum':        (pack_int, unpack_int),
        'pcName':         (_pack_str8_1, unpack_str8),
        'pcLevel':        (pack_short, unpack_short),
        'pcLocation':     (_pack_str8_1, unpack_str8),
        'gameDays':       (pack_float, unpack_float),
        'gameTicks':      (pack_int, unpack_int),
        'gameTime':       (_pack_string, _unpack_fstr16),
        'ssSize':         (pack_int, unpack_int),
        'ssWidth':        (pack_int, unpack_int),
        'ssHeight':       (pack_int, unpack_int),
    }

    def _write_masters(self, ins, out):
        #--Skip old masters
        ins.skip(1, 1)
        for x in range(len(self.masters)):
            _skip_str8(ins)
        #--Write new masters
        self.__write_masters_ob(out)
        #--Fids Address
        offset = out.tell() - ins.tell()
        fidsAddress = unpack_int(ins)
        pack_int(out, fidsAddress + offset)

    def __write_masters_ob(self, out):
        pack_byte(out, len(self.masters))
        for master_bstr in self.masters:
            pack_str8(out, master_bstr)

    def dump_header(self, out):
        out.write(self.__class__.save_magic)
        var_fields_size = 0
        for attr, (_pack, __unpack) in self._unpackers.items():
            ret = _pack(out, getattr(self, attr))
            if ret is not None:
                var_fields_size += ret
        # Update the header size before writing it out. Note that all fields
        # before saveNum do not count towards this
        # TODO(inf) We need a nicer way to do this (query size before dump) -
        #  ut: we need the binary string size here, header size must be
        #  updated when var fields change (like pcName)
        self.header_size = var_fields_size + 42 + len(self.ssData)
        self._mastersStart = out.tell()
        out.seek(34)
        self._unpackers['header_size'][0](out, self.header_size)
        out.seek(self._mastersStart)
        out.write(self.ssData)
        self._encode_masters()
        self.__write_masters_ob(out)
        self._decode_masters()

class _AEslSaveHeader(SaveFileHeader):
    """Base class for save headers that may have ESLs."""
    __slots__ = ('has_esl_masters', 'masters_regular', 'masters_esl')

    def _esl_block(self) -> bool:
        """Return True if this save file has an ESL block."""
        raise NotImplementedError

    @property
    def masters(self):
        return self.masters_regular + self.masters_esl

    def _load_masters(self, ins):
        # Skip super, that would try to load and assign self.masters
        self._load_from_unpackers(ins, self.__class__._unpackers_post_ss)
        self._mastersStart = ins.tell()
        self._load_masters_16(ins)

    def _load_masters_16(self, ins, sse_offset=0):
        """Load regular masters and ESL masters, with an optional offset for
        the compressed SSE saves."""
        masters_expected = unpack_int(ins)
        # Store separate lists of regular and ESLs masters for the Indices
        # column on the Saves tab
        num_regular_masters = unpack_byte(ins)
        self.masters_regular = [
            *map(unpack_str16, repeat(ins, num_regular_masters))]
        # SSE / FO4 save format with esl block
        if self._esl_block():
            num_esl_masters = unpack_short(ins)
            # Remember if we had ESL masters for the inaccuracy warning
            self.has_esl_masters = num_esl_masters > 0
            self.masters_esl = [
                *map(unpack_str16, repeat(ins, num_esl_masters))]
        else:
            self.has_esl_masters = False
            self.masters_esl = []
        # Check for master's table size (-4 for the stored size at the start of
        # the master table)
        masters_actual = ins.tell() + sse_offset - self._mastersStart - 4
        if masters_actual != masters_expected:
            raise SaveHeaderError(f'Save game masters size ({masters_actual}) '
                                  f'not as expected ({masters_expected}).')

    def _decode_masters(self):
        self.masters_regular = [FName(decoder(x, bolt.pluginEncoding,
            avoidEncodings=('utf8', 'utf-8'))) for x in self.masters_regular]
        self.masters_esl = [FName(decoder(x, bolt.pluginEncoding,
            avoidEncodings=('utf8', 'utf-8'))) for x in self.masters_esl]

    def _encode_masters(self):
        self.masters_regular = [encode(x) for x in self.masters_regular]
        self.masters_esl = [encode(x) for x in self.masters_esl]

    def remap_masters(self, master_map):
        self.masters_regular = [master_map.get(x, x)
                                for x in self.masters_regular]
        self.masters_esl = [master_map.get(x, x) for x in self.masters_esl]

    def _dump_masters(self, ins, out):
        # Skip the old masters
        reg_master_count = len(self.masters_regular)
        esl_master_count = len(self.masters_esl)
        for x in range(reg_master_count):
            _skip_str16(ins)
        # SSE/FO4 format has separate ESL block
        has_esl_block = self._esl_block()
        if has_esl_block:
            ins.seek(2, 1) # skip ESL count
            for count in range(esl_master_count):
                _skip_str16(ins)
        # Write out the (potentially altered) masters
        pack_byte(out, len(self.masters_regular))
        _write_s16_list(out, self.masters_regular)
        if has_esl_block:
            pack_short(out, len(self.masters_esl))
            _write_s16_list(out, self.masters_esl)

    def _master_block_size(self):
        return (3 if self._esl_block() else 1) + sum(
            len(x) + 2 for x in self.masters)

class SkyrimSaveHeader(_AEslSaveHeader):
    """Valid Save Game Versions 8, 9, 12 (?)"""
    save_magic = b'TESV_SAVEGAME'
    # extra slots - only version is really used, gameDate used once (calc_time)
    # _formVersion distinguish between old and new save formats
    # _compress_type in SSE saves - used to decide how to read/write them
    __slots__ = (u'gameDate', u'saveNumber', u'version', u'raceEid', u'pcSex',
                 u'pcExp', u'pcLvlExp', u'filetime', u'_formVersion',
                 'game_ver', '_compress_type', '_sse_start')

    _unpackers = {
        'header_size': (00, unpack_int),
        'version':     (00, unpack_int),
        'saveNumber':  (00, unpack_int),
        'pcName':      (00, unpack_str16),
        'pcLevel':     (00, unpack_int),
        'pcLocation':  (00, unpack_str16),
        'gameDate':    (00, unpack_str16),
        'raceEid':     (00, unpack_str16), # pcRace
        'pcSex':       (00, unpack_short),
        'pcExp':       (00, unpack_float),
        'pcLvlExp':    (00, unpack_float),
        'filetime':    (00, unpack_int64),
        'ssWidth':     (00, unpack_int),
        'ssHeight':    (00, unpack_int),
    }
    _unpackers_post_ss = {
        '_formVersion': (00, unpack_byte),
    }

    def __is_sse(self): return self.version == 12

    def _esl_block(self): return self.__is_sse() and self._formVersion >= 78

    @property
    def has_alpha(self):
        return self.__is_sse()

    def load_image_data(self, ins, load_image=False):
        if self.__is_sse():
            self._compress_type = _sc_parser[unpack_short(ins)]
        # -4 for the header size itself (uint32)
        actual = ins.tell() - len(self.__class__.save_magic) - 4
        if actual != self.header_size:
            raise SaveHeaderError(f'New Save game header size ({actual}) not '
                                  f'as expected ({self.header_size}).')
        super().load_image_data(ins, load_image)

    def _load_masters(self, ins):
        if (self.__is_sse() and
                self._compress_type is not _SaveCompressionType.NONE):
            self._sse_start = ins.tell()
            decompressed_size = unpack_int(ins)
            compressed_size = unpack_int(ins)
            sse_offset = ins.tell()
            ins = _decompress_save(ins, compressed_size, decompressed_size,
                self._compress_type, light_decompression=True)
        else:
            sse_offset = 0
        self._load_from_unpackers(ins, self.__class__._unpackers_post_ss)
        self._mastersStart = ins.tell() + sse_offset
        self._load_masters_16(ins, sse_offset)

    def calc_time(self):
        # gameDate format: hours.minutes.seconds
        hours, minutes, seconds = [int(x) for x in self.gameDate.split(b'.')]
        playSeconds = hours * 60 * 60 + minutes * 60 + seconds
        self.gameDays = float(playSeconds) / (24 * 60 * 60)
        self.gameTicks = playSeconds * 1000

    def _do_write_header(self, ins, out):
        if (not self.__is_sse() or
                self._compress_type is _SaveCompressionType.NONE):
            # Skyrim LE or uncompressed - can use the default implementation
            return super()._do_write_header(ins, out)
        # Write out everything up until the compressed portion
        out.write(ins.read(self._sse_start))
        # Now we need to decompress the portion again
        decompressed_size = unpack_int(ins)
        compressed_size = unpack_int(ins)
        ins = _decompress_save(ins, compressed_size, decompressed_size,
            self._compress_type)
        # Gather the data that will be compressed
        to_compress = io.BytesIO()
        pack_byte(to_compress, self._formVersion)
        ins.seek(1, 1) # skip the form version
        self._write_masters(ins, to_compress)
        for block in iter(partial(ins.read, 0x5000000), b''):
            to_compress.write(block)
        # Compress the gathered data, write out the sizes and finally write out
        # the actual compressed data
        compressed_data = _compress_save(to_compress, self._compress_type)
        pack_int(out, to_compress.tell())   # decompressed_size
        pack_int(out, len(compressed_data)) # compressed_size
        out.write(compressed_data)

class Fallout4SaveHeader(SkyrimSaveHeader): # pretty similar to skyrim
    """Valid Save Game Versions 11, 12, 13, 15 (?)"""
    save_magic = b'FO4_SAVEGAME'
    __slots__ = ('game_ver',)
    _unpackers_post_ss = {
        '_formVersion': (00, unpack_byte),
        'game_ver':     (00, unpack_str16),
    }
    _compress_type = _SaveCompressionType.NONE

    def _esl_block(self): return self.version == 15 and self._formVersion >= 68

    @property
    def has_alpha(self):
        return True

    def load_image_data(self, ins, load_image=False):
        # -4 for the header size itself (uint32)
        actual = ins.tell() - len(self.__class__.save_magic) - 4
        if actual != self.header_size:
            raise SaveHeaderError(f'New Save game header size ({actual}) not '
                                  f'as expected ({self.header_size}).')
        # Call the SaveFileHeader version, skip Skyrim
        super(SkyrimSaveHeader, self).load_image_data(ins, load_image)

    def calc_time(self):
        self.gameDays, self.gameTicks = calc_time_fo4(self.gameDate)

    def _do_write_header(self, ins, out):
        # Call the SaveFileHeader version - *not* the Skyrim one
        return super(SkyrimSaveHeader, self)._do_write_header(ins, out)

class _ABcpsSaveHeader(SaveFileHeader):
    """Base class for BCPS savegames, which are entirely compressed and
    preceded by a 'BCPS' header (Bethesda Compressed Something Something?)."""
    _bcps_magic = b'BCPS'
    ##: We can't fill these slots due to inheritance layout conflicts, so they
    # have to be filled out in subclasses - really annoying, plus causes
    # PyCharm warnings down below
    __slots__ = ()
    # TODO(SF) Before we can enable editing save headers, all the ##: questions
    #  here have to be answered
    _bcps_unpackers = {
        '_bcps_header_version': (00, unpack_int),
        ##: Where do we start counting for the 'header size' here?
        '_bcps_header_size':    (00, unpack_int64),
        ##: What are unknown1 and unknown2?
        '_bcps_unknown1':       (00, unpack_int64),
        '_bcps_comp_location':  (00, unpack_int64),
        ##: MO2 calls this 'totalSize', but it seems to be much too big for
        # either the compressed or decompressed size - what is it?
        '_bcps_unknown2':      (00, unpack_int64),
        '_bcps_unknown3':      (00, unpack_int64),
        '_bcps_decomp_size':   (00, unpack_int64),
        ##: There's a bunch more after this - read as _bcps_rest below. Figure
        # out what that all is
    }

    def load_header(self, ins, load_image=False):
        bcps_magic = ins.read(len(self.__class__._bcps_magic))
        if bcps_magic != self.__class__._bcps_magic:
            raise SaveHeaderError(f'Compressed header magic wrong: '
                                  f'{bcps_magic!r} (expected '
                                  f'{self.__class__._bcps_magic!r})')
        self._load_from_unpackers(ins, self.__class__._bcps_unpackers)
        self._bcps_rest = ins.read(self._bcps_comp_location - ins.tell())
        # We're at _bcps_comp_location now, no need to store that twice
        ins.seek(0, os.SEEK_END)
        ins_size = ins.tell()
        ins.seek(self._bcps_comp_location)
        ins = _decompress_save(ins, ins_size - self._bcps_comp_location,
            self._bcps_decomp_size, compression_type=_SaveCompressionType.ZLIB)
        super().load_header(ins, load_image)

    ##: Add writing support

class StarfieldSaveHeader(_ABcpsSaveHeader, _AEslSaveHeader):
    save_magic = b'SFS_SAVEGAME'
    __slots__ = ('version', 'unknown1', 'saveNumber', 'gameDate', 'raceEid',
                 'pcSex', 'pcExp', 'pcLvlExp', 'filetime', 'unknown3',
                 '_formVersion', 'game_ver', 'other_game_ver',
                 'plugin_info_size', '_bcps_header_version',
                 '_bcps_header_size', '_bcps_unknown1', '_bcps_comp_location',
                 '_bcps_unknown2', '_bcps_unknown3', '_bcps_decomp_size',
                 '_bcps_rest')

    # TODO(SF) What are the unknowns in here?
    _unpackers = {
        'header_size':      (00, unpack_int),
        'version':          (00, unpack_int),
        ##: Seems to be equal to _formVersion?
        'unknown1':         (00, unpack_byte),
        'saveNumber':       (00, unpack_int),
        'pcName':           (00, unpack_str16),
        'pcLevel':          (00, unpack_int),
        'pcLocation':       (00, unpack_str16),
        'gameDate':         (00, unpack_str16),
        'raceEid':          (00, unpack_str16), # pcRace
        'pcSex':            (00, unpack_short),
        'pcExp':            (00, unpack_float),
        'pcLvlExp':         (00, unpack_float),
        'filetime':         (00, unpack_int64),
        'ssWidth':          (00, unpack_int), ##: Seems unused - always(?) zero
        'ssHeight':         (00, unpack_int), ##: Seems unused - always(?) zero
        'unknown3':         (00, unpack_int), ##: Seems to always be 1?
    }
    _unpackers_post_ss = {
        '_formVersion':     (00, unpack_byte),
        'game_ver':         (00, unpack_str16),
        ##: Maybe this is the version the playthrough was started on, it seems
        # to not change when the game version is upgraded
        'other_game_ver':   (00, unpack_str16),
    }

    def _esl_block(self):
        return True # Some sources say if form version >= 82, MO2 says always

    def load_image_data(self, ins, load_image=False):
        # -4 for the header size itself (uint32)
        actual = ins.tell() - len(self.__class__.save_magic) - 4
        if actual != self.header_size:
            raise SaveHeaderError(f'New Save game header size ({actual}) not '
                                  f'as expected ({self.header_size}).')
        super().load_image_data(ins, load_image)

    def _load_masters(self, ins):
        self._load_from_unpackers(ins, self.__class__._unpackers_post_ss)
        self._mastersStart = ins.tell()
        # TODO(SF) For some reason we're off by 2 when reading, though we read
        #  the master list correctly - maybe there's a new block for the
        #  override-only plugins? There does seem to be a valid short after
        #  this, but it has a value of 1 and is followed by no valid plugin
        #  strings? For now, just HACK our way past the check via sse_offset
        ##: Starfield 1.9 made this worse. Since form version 109 (or 108, not
        # sure yet), another 4 unknown bytes (which seem to always be zero) got
        # added right before the unknown short and count as part of the
        # masters. Still no clue what any of that is for. 1.11 added another 14
        # bytes.
        if self._formVersion >= 119:
            offset = 20
        elif self._formVersion >= 109:
            offset = 6
        else:
            offset = 2
        self._load_masters_16(ins, sse_offset=offset)

    def calc_time(self):
        self.gameDays, self.gameTicks = calc_time_fo4(self.gameDate)

    def _do_write_header(self, ins, out):
        raise NotImplementedError # TODO(SF) Implement

class FalloutNVSaveHeader(SaveFileHeader):
    save_magic = b'FO3SAVEGAME'
    __slots__ = (u'language', u'save_number', u'pcNick', u'version',
                 u'gameDate')
    _masters_unknown_byte = 0x1B
    _unpackers = {
        'header_size': (00, unpack_int),
        'version':     (00, unpack_str_int_delim),
        'language':    (00, lambda ins: unpack_many(ins, '64sc')[0]),
        'ssWidth':     (00, unpack_str_int_delim),
        'ssHeight':    (00, unpack_str_int_delim),
        'save_number': (00, unpack_str_int_delim),
        'pcName':      (00, unpack_str16_delim),
        'pcNick':      (00, unpack_str16_delim),
        'pcLevel':     (00, unpack_str_int_delim),
        'pcLocation':  (00, unpack_str16_delim),
        'gameDate':    (00, unpack_str16_delim),
    }

    def _load_masters(self, ins):
        self._mastersStart = ins.tell()
        self._master_list_size(ins)
        self.masters = []
        numMasters = unpack_str_byte_delim(ins)
        for count in range(numMasters):
            self.masters.append(unpack_str16_delim(ins))

    def _master_list_size(self, ins):
        formVersion, masterListSize = unpack_many(ins, '=BI')
        if formVersion != self._masters_unknown_byte: raise SaveHeaderError(
            f'Unknown byte at position {ins.tell() - 4} is {formVersion!r} '
            f'not 0x{self._masters_unknown_byte:X}')
        return masterListSize

    def _write_masters(self, ins, out):
        self._master_list_size(ins) # discard old size
        pack_byte(out, self._masters_unknown_byte)
        pack_int(out, self._master_block_size())
        #--Skip old masters
        unpack_str_byte_delim(ins)
        self._dump_masters(ins, out)
        #--Offsets
        offset = out.tell() - ins.tell()
        #--File Location Table
        for i in range(5):
            # formIdArrayCount offset and 5 others
            oldOffset = unpack_int(ins)
            pack_int(out, oldOffset + offset)

    def _dump_masters(self, ins, out):
        for _x in range(len(self.masters)):
            unpack_str16_delim(ins)
        # Write new masters - note the silly delimiters
        pack_byte(out, len(self.masters))
        _pack_c(out, b'|')
        for master_bstr in self.masters:
            pack_short(out, len(master_bstr))
            _pack_c(out, b'|')
            out.write(master_bstr)
            _pack_c(out, b'|')

    def _master_block_size(self):
        return 2 + sum(len(x) + 4 for x in self.masters)

    def calc_time(self):
        # gameDate format: hours.minutes.seconds
        hours, minutes, seconds = [int(x) for x in self.gameDate.split(b'.')]
        playSeconds = hours * 60 * 60 + minutes * 60 + seconds
        self.gameDays = float(playSeconds) / (24 * 60 * 60)
        self.gameTicks = playSeconds * 1000

class Fallout3SaveHeader(FalloutNVSaveHeader):
    save_magic = b'FO3SAVEGAME'
    __slots__ = ()
    _masters_unknown_byte = 0x15
    _unpackers = copy.copy(FalloutNVSaveHeader._unpackers)
    del _unpackers['language']

class MorrowindSaveHeader(SaveFileHeader):
    """Morrowind saves are identical in format to record definitions.
    Accordingly, we delegate loading the header to our existing mod API."""
    save_magic = b'TES3'
    __slots__ = (u'pc_curr_health', u'pc_max_health')

    def load_header(self, ins, load_image=False):
        # TODO(inf) A bit ugly, this is not a mod - maybe move readHeader out?
        from . import ModInfo
        save_info = ModInfo(self._save_info.abs_path, load_cache=True)
        ##: Figure out where some more of these are (e.g. level)
        self.header_size = save_info.header.header.blob_size
        self.pcName = remove_newlines(save_info.header.pc_name)
        self.pcLevel = 0
        self.pcLocation = remove_newlines(save_info.header.curr_cell)
        self.gameDays = self.gameTicks = 0
        self.masters = save_info.masterNames[:]
        self.pc_curr_health = save_info.header.pc_curr_health
        self.pc_max_health = save_info.header.pc_max_health
        if load_image:
            # Read the image data - note that it comes as BGRA, which we
            # need to turn into RGB. Note that we disregard the alpha, seems to
            # make the image 100% black and is therefore unusable.
            out = io.BytesIO()
            for pxl in save_info.header.screenshot_data:
                out.write(
                    structs_cache[u'3B'].pack(pxl.red, pxl.green, pxl.blue))
            self.ssData = out.getvalue()
        self.ssHeight = self.ssWidth = 128 # fixed size for Morrowind

    @property
    def can_edit_header(self):
        # TODO(inf) Once we support writing Morrowind plugins, implement
        #  writeMasters properly and drop this override
        return False

# Factory
def get_save_header_type(game_fsName) -> type[SaveFileHeader]:
    match game_fsName:
        case ('Enderal' | 'Enderal Special Edition' | 'Skyrim' |
              'Skyrim Special Edition' | 'Skyrim VR'):
            return SkyrimSaveHeader
        case 'Fallout3':
            return Fallout3SaveHeader
        case 'Fallout4' | 'Fallout4VR':
            return Fallout4SaveHeader
        case 'FalloutNV':
            return FalloutNVSaveHeader
        case 'Morrowind':
            return MorrowindSaveHeader
        case 'Oblivion':
            return OblivionSaveHeader
        case 'Starfield':
            return StarfieldSaveHeader
        case _:
            raise RuntimeError(f'Save header decoding is not supported for '
                               f'{game_fsName} yet')

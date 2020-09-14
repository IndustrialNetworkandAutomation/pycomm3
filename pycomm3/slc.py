# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 Ian Ottoway <ian@ottoway.dev>
# Copyright (c) 2014 Agostino Ruscito <ruscito@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

__all__ = ['SLCDriver', ]

import logging
import math
import re
from typing import List, Tuple, Optional, Union

from .bytes_ import Pack, Unpack
from .cip_base import CIPDriver, with_forward_open
from .const import (CLASS_TYPE, SUCCESS, PCCC_CT, PCCC_DATA_TYPE, PCCC_DATA_SIZE, PCCC_ERROR_CODE,
                    SLC_CMD_CODE, SLC_FNC_READ, SLC_FNC_WRITE, SLC_REPLY_START, PCCC_PATH)
from .exceptions import DataError, RequestError
from .tag import Tag
from .packets.requests import wrap_unconnected_send
AtomicValueType = Union[int, float, bool]
TagValueType = Union[AtomicValueType, List[Union[AtomicValueType, str]]]
ReadWriteReturnType = Union[Tag, List[Tag]]


class SLCDriver(CIPDriver):
    """
    An Ethernet/IP Client driver for reading and writing of data files in SLC or MicroLogix PLCs.
    """
    __log = logging.getLogger(f'{__module__}.{__qualname__}')

    def __init__(self, path, *args, **kwargs):
        super().__init__(path, *args, large_packets=False, **kwargs)

    def _msg_start(self):
        """
        No idea what this part is, but it starts all messages
        """
        return b''.join((
            b'\x4b',
            b'\x02',
            CLASS_TYPE["8-bit"],
            PCCC_PATH,
            b'\x07',
            self._cfg['vid'],
            self._cfg['vsn'],
        ))

    @with_forward_open
    def read(self, *addresses: str) -> ReadWriteReturnType:
        """
        Reads data file addresses. To read multiple words add the word count to the address using curly braces,
        e.g. ``N120:10{10}``.

        Does not track request/response size like the CLXDriver.

        :param addresses: one or many data file addresses to read
        :return: a single or list of ``Tag`` objects
        """
        results = [self._read_tag(tag) for tag in addresses]

        if len(results) == 1:
            return results[0]

        return results

    def _read_tag(self, tag) -> Tag:
        _tag = parse_tag(tag)
        if _tag is None:
            raise RequestError(f"Error parsing the tag passed to read() - {tag}")

        message_request = [
            self._msg_start(),

            # page 83 of eip manual
            SLC_CMD_CODE,  # request command code
            b'\x00',  # status code
            Pack.uint(self._sequence),  # transaction identifier
            SLC_FNC_READ,  # function code
            Pack.usint(PCCC_DATA_SIZE[_tag['file_type']] * _tag['element_count']),  # byte size
            Pack.usint(int(_tag['file_number'])),
            PCCC_DATA_TYPE[_tag['file_type']],
            Pack.usint(int(_tag['element_number'])),
            Pack.usint(int(_tag.get('pos_number', 0)))  # sub-element number
        ]

        request = self.new_request('send_unit_data')
        request.add(b''.join(message_request))
        response = request.send()
        self.__log.debug(f"SLC read_tag({tag})")

        status = request_status(response.raw)
        if status is not None:
            return Tag(_tag['tag'], None, _tag['file_type'], status)

        try:
            return _parse_read_reply(_tag, response.raw[SLC_REPLY_START:])
        except DataError as err:
            self.__log.exception(f'Failed to parse read reply for {_tag["tag"]}')
            return Tag(_tag['tag'], None, _tag['file_type'], str(err))

    @with_forward_open
    def write(self, *address_values: Tuple[str, TagValueType]) -> ReadWriteReturnType:
        """
        Write values to data file addresses.  To write to multiple words in a file use curly braces in the address
        to indicate the number of words, then set the value to a list of values to write e.g. ``('N120:10{10}', [1, 2, ...])``.

        Does not track request/response size like the CLXDriver.


        :param address_values: one or many 2-element tuples of (address, value)
        :return: a single or list of ``Tag`` objects
        """
        results = [self._write_tag(tag, value) for tag, value in address_values]

        if len(results) == 1:
            return results[0]

        return results

    def _write_tag(self, tag: str, value: TagValueType) -> Tag:
        """ write tag from a connected plc
        Possible combination can be passed to this method:
            c.write_tag('N7:0', [-30, 32767, -32767])
            c.write_tag('N7:0', 21)
            c.read_tag('N7:0', 10)
        It is not possible to write status bit
        :return: None is returned in case of error
        """
        _tag = parse_tag(tag)
        if _tag is None:
            raise RequestError(f"Error parsing the tag passed to write() - {tag}")

        _tag['data_size'] = PCCC_DATA_SIZE[_tag['file_type']]

        message_request = [
            self._msg_start(),

            SLC_CMD_CODE,
            b'\x00',
            Pack.uint(self._sequence),
            SLC_FNC_WRITE,
            Pack.usint(_tag['data_size'] * _tag['element_count']),
            Pack.usint(int(_tag['file_number'])),
            PCCC_DATA_TYPE[_tag['file_type']],
            Pack.usint(int(_tag['element_number'])),
            Pack.usint(int(_tag.get('pos_number', 0))),
            writeable_value(_tag, value),
        ]
        request = self.new_request('send_unit_data')
        request.add(b''.join(message_request))
        response = request.send()

        status = request_status(response.raw)
        if status is not None:
            return Tag(_tag['tag'], None, _tag['file_type'], status)

        return Tag(_tag['tag'], value, _tag['file_type'], None)

    @with_forward_open
    def get_processor_type(self):
        msg_request = (
            self._msg_start(),
            # diagnostic status - CMD 06, FNC 03 - pg93 DF1 manual (1770-rm516)
            b'\x06',  # CMD
            b'\x00',  # status code
            Pack.uint(self._sequence),  # transaction identifier
            b'\x03',  # FNC

        )

        request = self.new_request('send_unit_data')
        request.add(b''.join(msg_request))
        response = request.send()
        if response:
            try:
                typ = response.raw[SLC_REPLY_START:][5:16].decode('utf-8').strip()
            except Exception as err:
                self.__log.exception('failed getting processor type')
                typ = None
            finally:
                return typ
        else:
            self.__log.error(f'failed to get processor type: {request_status(response.raw)}',)
            return None

    @with_forward_open
    def get_file_directory(self):
        plc_type = self.get_processor_type()

        if plc_type is not None:
            file_type, element = _get_file_and_element_for_plc_type(plc_type)
            size = self._get_file_directory_size(file_type, element)

            if size is not None:
                data = self._read_whole_file_directory(file_type, element, size)
                return _parse_file0(plc_type, data)
            else:
                raise DataError('Failed to read file directory size')
        else:
            raise DataError('Failed to read processor type')

    def _get_file_directory_size(self, file_type, element):
        msg_request = [
            self._msg_start(),

            SLC_CMD_CODE,  # request command code
            b'\x00',  # status code
            Pack.uint(self._sequence),  # transaction identifier
            b'\xa1',  # function code, from RSLinx capture
            b'\x04',  # size
            b'\x00',  # file number
            file_type,
            element,
        ]

        request = self.new_request('send_unit_data')
        request.add(b''.join(msg_request))
        response = request.send()
        status = request_status(response.raw)
        if status is None:
            try:
                size = Unpack.uint(response.raw[SLC_REPLY_START:])
            except Exception as err:
                self.__log.exception('failed to parse size of File 0')
                size = None
            finally:
                return size
        else:
            self.__log.error(f'failed to read size of File 0: {status}', )
            return None

    def _read_whole_file_directory(self, file_type,  element, file0_size):
        file0_data = b''
        offset = 0

        while len(file0_data) < file0_size:
            bytes_remaining = file0_size - len(file0_data)
            size = 0x50 if bytes_remaining > 0x50 else bytes_remaining

            msg_request = [
                self._msg_start(),
                # page 83 of eip manual
                SLC_CMD_CODE,  # request command code
                b'\x00',  # status code
                Pack.uint(self._sequence),  # transaction identifier
                SLC_FNC_READ,  # function code
                Pack.usint(size),  # size
                b'\x00',  # file number
                file_type,
                ]

            msg_request += [b'\x00', Pack.usint(offset)] if offset < 256 else [b'\xFF', Pack.uint(offset)]

            request = self.new_request('send_unit_data')
            request.add(b''.join(msg_request))
            response = request.send()
            status = request_status(response.raw)
            if status is None:
                data = response.raw[SLC_REPLY_START:]
                offset += len(data) // 2
                file0_data += data
            else:
                msg = f'Error reading File 0 contents: {status}'
                self.__log.error(msg)
                raise DataError(msg)

        return file0_data


def _parse_file0(plc_type, data):
    num_data_files = data[52]
    num_lad_files = data[46]
    print(f'data files: {num_data_files}, logic files: {num_lad_files}')
    file_pos, row_size = _get_file_position_row_size_for_plc_type(plc_type)

    data_files = {}
    file_num = 0
    while file_pos < len(data):
        file_code = data[file_pos:file_pos + 1]
        file_type = PCCC_DATA_TYPE.get(file_code, None)

        if file_type:
            file_name = f'{file_type}{file_num}'

            element_size = PCCC_DATA_SIZE.get(file_type, 2)
            file_size = Unpack.uint(data[file_pos+1:])
            data_files[file_name] = {'elements': file_size//element_size, 'length': file_size, }

        if file_type or file_code == b'\x81':  # 0x81 reserved type, for skipped file numbers?
            file_num += 1

        file_pos += row_size

    return data_files


def _get_file_and_element_for_plc_type(plc_type):
    if plc_type in {'5/02', }:  # TODO: add MLX1000
        file_type = b'\x00'
        element = b'\x23'
    elif plc_type in {}:  # TODO: add MLX1100/1200/1500
        file_type = b'\x02'
        element = b'\x2F'
    else:
        file_type = b'\x01'
        element = b'\x23'

    return file_type, element


def _get_file_position_row_size_for_plc_type(plc_type):
    prefix = plc_type[:4]
    if prefix in {'5/02', '1761'}:  # SLC 5/02 MLX1000
        file_pos = 93
        row_size = 8
    elif prefix in {'1763', '1762', '1764'}:  # MLX1100/1200/1500
        file_pos = 103
        row_size = 10
    else:  # other SLCs or MLX1400
        file_pos = 79
        row_size = 10

    return file_pos, row_size


def _parse_read_reply(tag, data) -> Tag:
    try:
        bit_read = tag.get('address_field', 0) == 3
        bit_position = int(tag.get('sub_element') or 0)
        data_size = PCCC_DATA_SIZE[tag['file_type']]
        unpack_func = Unpack[f'pccc_{tag["file_type"].lower()}']
        if bit_read:
            new_value = 0
            if tag['file_type'] in {'T', 'C'}:
                if bit_position == PCCC_CT['PRE']:
                    return Tag(tag['tag'],
                               unpack_func(data[new_value + 2:new_value + 2 + data_size]),
                               tag['file_type'],
                               None)

                elif bit_position == PCCC_CT['ACC']:
                    return Tag(tag['tag'],
                               unpack_func(data[new_value + 4:new_value + 4 + data_size]),
                               tag['file_type'],
                               None)

            tag_value = unpack_func(data[new_value:new_value + data_size])
            return Tag(tag['tag'],
                       get_bit(tag_value, bit_position),
                       tag['file_type'],
                       None)

        else:
            values_list = [unpack_func(data[i: i + data_size])
                           for i in range(0, len(data), data_size)]
            if len(values_list) > 1:
                return Tag(tag['tag'], values_list, tag['file_type'], None)
            else:
                return Tag(tag['tag'], values_list[0], tag['file_type'], None)
    except Exception as err:
        raise DataError('Failed parsing tag read reply') from err


def parse_tag(tag: str) -> Optional[dict]:
    t = re.search(r"(?P<file_type>[CT])(?P<file_number>\d{1,3})"
                  r"(:)(?P<element_number>\d{1,3})"
                  r"(.)(?P<sub_element>ACC|PRE|EN|DN|TT|CU|CD|DN|OV|UN|UA)",
                  tag, flags=re.IGNORECASE)
    if (
            t
            and (1 <= int(t.group('file_number')) <= 255)
            and (0 <= int(t.group('element_number')) <= 255)
    ):
        return {'file_type': t.group('file_type').upper(),
                'file_number': t.group('file_number'),
                'element_number': t.group('element_number'),
                'sub_element': PCCC_CT[t.group('sub_element').upper()],
                'address_field': 3,
                'element_count': 1,
                'tag': t.group(0)}

    t = re.search(r"(?P<file_type>[LFBN])(?P<file_number>\d{1,3})"
                  r"(:)(?P<element_number>\d{1,3})"
                  r"(/(?P<sub_element>\d{1,2}))?"
                  r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
                  tag, flags=re.IGNORECASE)
    if t:
        _cnt = t.group('_elem_cnt_token')
        tag_name = t.group(0).replace(_cnt, '') if _cnt else t.group(0)

        if t.group('sub_element') is not None:
            if (
                    (1 <= int(t.group('file_number')) <= 255)
                    and (0 <= int(t.group('element_number')) <= 255)
                    and (0 <= int(t.group('sub_element')) <= 15)
            ):
                element_count = t.group('element_count')
                return {'file_type': t.group('file_type').upper(),
                        'file_number': t.group('file_number'),
                        'element_number': t.group('element_number'),
                        'sub_element': t.group('sub_element'),
                        'address_field': 3,
                        'element_count': int(element_count) if element_count is not None else 1,
                        'tag': tag_name}
        else:
            if (
                    (1 <= int(t.group('file_number')) <= 255)
                    and (0 <= int(t.group('element_number')) <= 255)
            ):
                element_count = t.group('element_count')
                return {'file_type': t.group('file_type').upper(),
                        'file_number': t.group('file_number'),
                        'element_number': t.group('element_number'),
                        'sub_element': t.group('sub_element'),
                        'address_field': 2,
                        'element_count': int(element_count) if element_count is not None else 1,
                        'tag': tag_name}

    t = re.search(r"(?P<file_type>[IO])(:)(?P<element_number>\d{1,3})"
                  r"(.)(?P<position_number>\d{1,3})"
                  r"(/(?P<sub_element>\d{1,2}))?"
                  r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
                  tag, flags=re.IGNORECASE)
    if t:
        _cnt = t.group('_elem_cnt_token')
        tag_name = t.group(0).replace(_cnt, '') if _cnt else t.group(0)
        if t.group('sub_element') is not None:
            if (
                    (0 <= int(t.group('file_number')) <= 255)
                    and (0 <= int(t.group('element_number')) <= 255)
                    and (0 <= int(t.group('sub_element')) <= 15)
            ):
                element_count = t.group('element_count')
                return {'file_type': t.group('file_type').upper(),
                        'file_number': '0',
                        'element_number': t.group('element_number'),
                        'pos_number': t.group('position_number'),
                        'sub_element': t.group('sub_element'),
                        'address_field': 3,
                        'element_count': int(element_count) if element_count is not None else 1,
                        'tag': tag_name}
        else:
            if 0 <= int(t.group('element_number')) <= 255:
                element_count = t.group('element_count')
                return {'file_type': t.group('file_type').upper(),
                        'file_number': '0',
                        'element_number': t.group('element_number'),
                        'pos_number': t.group('position_number'),
                        'address_field': 2,
                        'element_count': int(element_count) if element_count is not None else 1,
                        'tag': tag_name}

    t = re.search(r"(?P<file_type>ST)(?P<file_number>\d{1,3})"
                  r"(:)(?P<element_number>\d{1,4})"
                  r"(?P<_elem_cnt_token>{(?P<element_count>[12])})?",
                  tag, flags=re.IGNORECASE)
    if (
            t
            and (1 <= int(t.group('file_number')) <= 255)
            and (0 <= int(t.group('element_number')) <= 255)
    ):
        _cnt = t.group('_elem_cnt_token')
        tag_name = t.group(0).replace(_cnt, '') if _cnt else t.group(0)
        element_number = int(t.group('element_number'))
        element_count = t.group('element_count')
        return {'file_type': t.group('file_type').upper(),
                'file_number': t.group('file_number'),
                'element_number': element_number,
                'address_field':  2,
                'element_count': int(element_count) if element_count is not None else 1,
                'tag': tag_name}

    t = re.search(r"(?P<file_type>A)(?P<file_number>\d{1,3})"
                  r"(:)(?P<element_number>\d{1,4})"
                  r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
                  tag, flags=re.IGNORECASE)
    if (
            t
            and (1 <= int(t.group('file_number')) <= 255)
            and (0 <= int(t.group('element_number')) <= 255)
    ):
        _cnt = t.group('_elem_cnt_token')
        tag_name = t.group(0).replace(_cnt, '') if _cnt else t.group(0)
        element_number = int(t.group('element_number'))
        element_count = t.group('element_count')
        return {'file_type': t.group('file_type').upper(),
                'file_number': t.group('file_number'),
                'element_number': element_number,
                'address_field': 2,
                'element_count': int(element_count) if element_count is not None else 1,
                'tag': tag_name}

    t = re.search(r"(?P<file_type>S)"
                  r"(:)(?P<element_number>\d{1,3})"
                  r"(/(?P<sub_element>\d{1,2}))?"
                  r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
                  tag, flags=re.IGNORECASE)
    if t:
        _cnt = t.group('_elem_cnt_token')
        tag_name = t.group(0).replace(_cnt, '') if _cnt else t.group(0)
        element_count = t.group('element_count')
        if t.group('sub_element') is not None:
            if (
                    (0 <= int(t.group('element_number')) <= 255)
                    and (0 <= int(t.group('sub_element')) <= 15)
            ):
                return {'file_type': t.group('file_type').upper(),
                        'file_number': '2',
                        'element_number': t.group('element_number'),
                        'sub_element': t.group('sub_element'),
                        'address_field': 3,
                        'element_count': int(element_count) if element_count is not None else 1,
                        'tag': t.group(0)}
        else:
            if 0 <= int(t.group('element_number')) <= 255:
                return {'file_type': t.group('file_type').upper(),
                        'file_number': '2',
                        'element_number': t.group('element_number'),
                        'address_field': 2,
                        'element_count': int(element_count) if element_count is not None else 1,
                        'tag': tag_name}

    t = re.search(r"(?P<file_type>B)(?P<file_number>\d{1,3})"
                  r"(/)(?P<element_number>\d{1,4})"
                  r"(?P<_elem_cnt_token>{(?P<element_count>\d+)})?",
                  tag, flags=re.IGNORECASE)
    if (
            t
            and (1 <= int(t.group('file_number')) <= 255)
            and (0 <= int(t.group('element_number')) <= 4095)
    ):
        _cnt = t.group('_elem_cnt_token')
        tag_name = t.group(0).replace(_cnt, '') if _cnt else t.group(0)
        bit_position = int(t.group('element_number'))
        element_number = bit_position / 16
        sub_element = bit_position - (element_number * 16)
        element_count = t.group('element_count')
        return {'file_type': t.group('file_type').upper(),
                'file_number': t.group('file_number'),
                'element_number': element_number,
                'sub_element': sub_element,
                'address_field': 3,
                'element_count': int(element_count) if element_count is not None else 1,
                'tag': tag_name}



    return None


def get_bit(value: int, idx: int) -> bool:
    """:returns value of bit at position idx"""
    return (value & (1 << idx)) != 0


def writeable_value(tag: dict, value: Union[bytes, TagValueType]) -> bytes:
    if isinstance(value, bytes):
        return value
    bit_field = tag.get('address_field', 0) == 3
    bit_position = int(tag.get('sub_element') or 0) if bit_field else 0

    element_count = tag.get('element_count') or 1
    if element_count > 1:
        if len(value) < element_count:
            raise RequestError(
                f'Insufficient data for requested elements, expected {element_count} and got {len(value)}')
        if len(value) > element_count:
            value = value[:element_count]

    try:
        pack_func = Pack[f'pccc_{tag["file_type"].lower()}']

        if element_count > 1:
            _value = b''.join(pack_func(val) for val in value)
        else:
            if bit_field:
                tag['data_size'] = 2

                if tag['file_type'] in ['T', 'C'] and bit_position in {
                    PCCC_CT['PRE'],
                    PCCC_CT['ACC'],
                }:
                    _value = b'\xff\xff' + pack_func(value)
                else:
                    if value > 0:
                        _value = Pack.uint(math.pow(2, bit_position)) + Pack.uint(math.pow(2, bit_position))
                    else:
                        _value = Pack.uint(math.pow(2, bit_position)) + Pack.uint(0)

            else:
                _value = pack_func(value)

    except Exception as err:
        raise RequestError(f'Failed to create a writeable value for {tag["tag"]} from {value}') from err

    else:
        return _value


def request_status(data) -> Optional[str]:
    try:
        _status_code = int(data[58])
        if _status_code == SUCCESS:
            return None
        return PCCC_ERROR_CODE.get(_status_code, 'Unknown Status')
    except Exception:
        return 'Unknown Status'

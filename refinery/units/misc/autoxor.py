#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re

from refinery.units.misc.xkey import xkey
from refinery.lib.mime import FileMagicInfo as magic


class autoxor(xkey, extend_docs=True):
    """
    Assumes input that was encrypted with a polyalphabetic block cipher, like XOR-ing each byte
    with successive bytes from a key or by subtracting the respective key byte value from each
    input byte. It uses the `refinery.xkey` unit to attack the cipher and attempts to recover the
    plaintext automatically.
    """
    def process(self, data: bytearray):
        for result in self._attack(data):
            key = result.key
            names = [how] if (how := result.cipher) else list(self._METHODS)
            break
        else:
            self.log_warn('No key was found; returning original data.')
            return data

        fallback = None

        for name in names:

            unit = self._method(name)
            bin, = data | unit(key)
            space = B'\0' | unit(0x20) | bytes

            if not (m := magic(bin)).blob:
                self.log_info(F'method {name} resulted in {m.mime} data; returning buffer')
                return self.labelled(bin, key=key, method=name)
            if fallback is None:
                fallback = name, key, bin
            if not any(bin):
                continue

            as_text = bin | unit(space) | bytearray

            try:
                decoded = as_text.decode('utf8')
            except UnicodeDecodeError:
                is_text = False
            else:
                is_text = bool(re.fullmatch(r'[\s\w!-~]+', decoded))

            if is_text:
                self.log_info('detected likely text input; automatically shifting towards space character')
                key = (b'\x20' * len(key)) | unit(key) | bytes
                return self.labelled(as_text, key=key, method=name)

        if fallback:
            self.log_warn('unrecognized format and no confirmed crib; the output is likely junk')
            name, key, bin = fallback
            return self.labelled(bin, key=key)

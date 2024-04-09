#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from .. import TestUnitBase


class TestNRV(TestUnitBase):

    def test_decompress_nrv2d(self):
        import json
        data = self.download_sample('c5dfe786ecdb5b8b3824ab3e774b08ceb617d33efa54826375f80f166724188d')
        test = data | self.load_pipeline(
            'b64 | struct {n:I}{k:128}{:n} [| rc4 eat:k | snip 10: | nrv2d | repl -n1 H:0D0A MZ | repl -n1 H:0D0A PE | pemeta ]]')
        test = test | json.loads
        self.assertEqual(test['TimeStamp']['Linker'], '2021-08-12 13:49:29')

    def test_decompress_nrv2e(self):
        import base64
        test = base64.b64decode(
            'BgQAANtYAAJDAPkxAHwAQXIw7zcGNN4ANiox+w81HrUGOP8eUABSAEUA+1oAWQBEDv9OAFQAIABN3wAuClMAvlQPV/eKUhq9Wg5X'
            '7k58UtcWSVq9TF5J79pBZ+5PAEsG12bTSm5GVQBM/ntSAEH7L1dj+0MAS1vvMvovewo3Ut4wDi39HjEAN6Pbl0FNe3YgPt5Q3kv3'
            'IlSevVnX1z9FMmuCShL2WgBaG9umKADvSAApJnx75k+itwZMAEx9X0rvbkSOTXtOOF/DRy0WOW53fPYLFoMzLr0xAi3DGnevLQOC'
            'fJ/vQZ5TcBZrN0oa9k4AfA82Q4QaDzj3q8deN6sN7zIE/1x8lbMnQdwBQi5ZT86jL2tqNAr2MwAw34xSH+uPSVPYFxZThBMzON8A'
            'MJM5wQA3MwRcMX7bNcET2jInwyedE01HZ4dlM94qKy0DL38fNgAqeBszSxOvNIeKfHM7fCLxNQAwVkMtdzl7Xiw/YMyrFzxQACBW'
            'w+Hza7c3C93/NWuHg1OWRquPQ5KP02K9IBZT4QZC9oNZU7aXFiOX83U4ADJFC7ADhrNVCyOW8w9qMbEnZhdHbHxjdjIT7E4DW0M3'
            'OQuGaxYmCSSSSSr/')
        goal = base64.b64decode(
            'WABYAEMAMQB8AEEAQQBBADAAMAAwADcAMAA0ADYAfAAxADQANgA1ADAANQA4AHwAUABSAEUAWgBZAEQARQBOAFQAIABNAC4AIABT'
            'AFQALgAgAFcAQQBSAFMAWgBBAFcAWQB8AEQAWgBJAEUATABOAEkAQwBBACAATQBPAEsATwBUANMAVwB8AFUATAAuACAAUgBBAEsA'
            'TwBXAEkARQBDAEsAQQAgADIANQAvADIANwB8ADAAMgAtADUAMQA3ACAAVwBBAFIAUwBaAEEAVwBBAHwARABNAEkAIAAxAFAATgBL'
            'AHwAVABPAFkATwBUAEEAfABFADEAMgBKAHwAWgBaAEUAMQAyADAAKABIACkAfAB8AEMATwBSAE8ATABMAEEAfABKAFQARABLAE0A'
            'MgA4AEUAMQAwADAAMAA4ADkAMQAyADAAfAAyADAAMQAzAC0AMQAxAC0AMAA2AHwALQAtAC0AfABLAE8AVwBBAEwAUwBLAEkAIABK'
            'AEEATgB8AEoAQQBOAHwASwBPAFcAQQBMAFMASwBJAHwAfAA4ADIAMAA5ADEANwAxADEAMAAyADIAfAAwADIALQA1ADEANwB8AFcA'
            'QQBSAFMAWgBBAFcAQQB8AHwAVwBBAEEBQgBSAFoAWQBTAEsAQQB8ADIANAB8ADMAMAB8AEsATwBXAEEATABTAEsAQQAgAE0AQQBS'
            'AEkAQQB8AE0AQQBSAEkAQQB8AEsATwBXAEEATABTAEsAQQB8AHwAOAA4ADAAMwAwADkANwAxADAAMgAyAHwAMAAyAC0ANQAxADcA'
            'fABXAEEAUgBTAFoAQQBXAEEAfAB8AFcAQQBBAUIAUgBaAFkAUwBLAEEAfAAyADQAfAAzADAAfAAxADYANQA1AHwAMQA2ADUANQB8'
            'ADIANgA1ADUAfAAxADIAMAA1AHwATQAxAHwAZQAxADEAKgAyADAAMAAxAC8AMQAxADYAKgAwADEAOAAwACoAMAA0AHwAMgB8ADEA'
            'MAAwADAAfAA0ADUAMAB8AC0ALQAtAHwAMQAzADkAOAAsADAAMAB8ADcAMQAsADAAMAB8AFAAIAB8ADIAMAAwADUALQAwADcALQAw'
            'ADEAfAA1AHwALQAtAC0AfABTAEEATQBPAEMASADTAEQAIABPAFMATwBCAE8AVwBZAHwALQAtAC0AfAAyADAAMAA1AHwALQAtAC0A'
            'fAA4ACwAOAAyAHwAQQBBAEEAMAAwADAAMAAwADAAMAB8ADAAMgA2ADUAMAAwADAAOAAwADAAMAAxADUAOAB8ADAAMwB8ADAAMgB8'
            'ADAAMAAwAHwAMgAwADAAMABOAE4ATgBOAE4ATgBOAE4AfAAwADAAOQAwADAAMgAwADAAMQB8AA==')
        self.assertEqual(test | self.ldu('nrv2e', 8) | bytes, goal)


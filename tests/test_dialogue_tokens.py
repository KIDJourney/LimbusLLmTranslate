#!/usr/bin/env python3
"""Regression test for rich-text tag tokens vs natural dialogue in angle brackets."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import localization as loc


class TestDialogueTokens(unittest.TestCase):
    def test_natural_dialogue_in_angle_brackets_passes(self):
        # Real shard08 item 599 case
        source_599 = "<N사에서 살아온, 그러다 추방된… 내게 조언을 아끼지 않고 기억력이 좋은…>"
        trans_599 = "<在N公司生活过，随后被驱逐出境……毫不吝啬向我提出建议且记忆力极佳的……>"
        loc.validate_translation(source_599, trans_599)

        # Other Korean text in angle brackets with English prefix or numbers
        dialogues = [
            (
                "<DAY ■■, ■■ ■■ 연구 용역 결과 보고서>",
                "<DAY ■■, ■■ ■■ 研究委托结果报告书>",
            ),
            (
                "<A안…?>",
                "<A计划……？>",
            ),
            (
                "<T사 기술청 직원들이 여기까지 쫓아온 거야?>",
                "<T公司技术厅的员工们都追到这里来了吗？>",
            ),
            (
                "<W사에서 의심이 가는 것들에 대한 걸 공유해주진 않은 거야?>",
                "<W公司没提供什么信息以助于我们开始行动吗？>",
            ),
            (
                "<SPA관이랑 다르게 이곳의 옷들은 전체적으로 느낌이 비슷하네.>",
                "<和SPA馆不同，这里的衣服整体风格都差不多呢。>",
            ),
        ]
        for src, trans in dialogues:
            with self.subTest(src=src):
                loc.validate_translation(src, trans)

    def test_real_html_and_unity_tags_strictly_enforced(self):
        # Correctly preserved tags
        valid_pairs = [
            (
                "Hello <color=#ff0000>Red</color> world",
                "你好 <color=#ff0000>红</color> 世界",
            ),
            (
                "<mark color=#ff000040><b><u><customTag>Test</customTag></u></b></mark>",
                "<mark color=#ff000040><b><u><customTag>测试</customTag></u></b></mark>",
            ),
            (
                "<size=120%>Big</size> <size=\"60%\">Small</size>",
                "<size=120%>大</size> <size=\"60%\">小</size>",
            ),
            (
                "<ruby=Fabius>파비우스</ruby>",
                "<ruby=Fabius>法比乌斯</ruby>",
            ),
            (
                "<sprite name=\"AaCePbBa\"> icon",
                "<sprite name=\"AaCePbBa\"> 图标",
            ),
        ]
        for src, trans in valid_pairs:
            with self.subTest(src=src):
                loc.validate_translation(src, trans)

        # Altered or removed tags must raise ValueError
        invalid_pairs = [
            (
                "Hello <color=#ff0000>Red</color> world",
                "你好 <color=#00ff00>红</color> 世界",  # color changed
            ),
            (
                "Hello <color=#ff0000>Red</color> world",
                "你好 红 世界",  # tag removed
            ),
            (
                "<b>Bold</b> and <i>Italic</i>",
                "<b>粗体</b> 和 <b>斜体</b>",  # tag mismatched
            ),
            (
                "<mark color=#ff000040><b><u>Test</u></b></mark>",
                "<mark color=#ff000040><b>Test</b></mark>",  # dropped <u>
            ),
            (
                "<customTag attr=\"val\">Test</customTag>",
                "<customTag>Test</customTag>",  # dropped attribute
            ),
        ]
        for src, trans in invalid_pairs:
            with self.subTest(src=src, trans=trans):
                with self.assertRaises(ValueError):
                    loc.validate_translation(src, trans)

    def test_tokens_findall_isolation(self):
        source = "<N사에서 살아온...><color=#123456>{0}[Token]%s</color>"
        tokens = loc.TOKENS.findall(source)
        self.assertEqual(tokens, ["<color=#123456>", "{0}", "[Token]", "%s", "</color>"])


if __name__ == "__main__":
    unittest.main()

from llm import chat_with_coze, BOT_ID_TRANSLATE
import os
import sys
from path import CN_FILES_BASE_NAME_MAPPING, KR_FILES_BASE_NAME_MAPPING, read_file_data, write_file_data, to_cn_path
from keywords_const import STATIC_KEYWORDS_MAPPING


key_to_translate = ['place', 'content']
file_to_translate = ['S832B.json', 'S833A.json','S833B.json', 'S833I1.json', 'S833I2.json', 'S833I3.json', 'S833I4.json'][:1]
cached = {}


if __name__ == "__main__":

    for f in file_to_translate:
        kr_file_path = KR_FILES_BASE_NAME_MAPPING[f]

        kr_data = read_file_data(kr_file_path)

        for item in kr_data:
            for key in key_to_translate:
                if key in item:
                    text = item[key].strip()
                    if not text.strip():
                        continue

                    print(f"Translating {key} in {f}: {text}")
                    translated_text = "not_work"
                    if text in cached:
                        translated_text = cached[text]
                    else :
                        for static_keyword, value in STATIC_KEYWORDS_MAPPING.items():
                            text.replace(static_keyword, value)

                        translated_text = chat_with_coze(BOT_ID_TRANSLATE, text)
                        cached[text] = translated_text

                    item[key] = translated_text
                    print(f"Translated {key} in {f}: {translated_text}")
                    print("="*100)
                    break
            break   

        cn_file_path = to_cn_path(kr_file_path)
        write_file_data(cn_file_path, kr_data)
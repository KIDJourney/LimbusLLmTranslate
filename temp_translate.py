from llm import chat_with_coze, BOT_ID_TRANSLATE
import os
import sys
from path import CN_FILES_BASE_NAME_MAPPING, KR_FILES_BASE_NAME_MAPPING, read_file_data, write_file_data, to_cn_path
from keywords_const import STATIC_KEYWORDS_MAPPING, LLM_KEYWORDS_MAPPING


key_to_translate = ['place', 'content']
file_to_translate = ['S832B.json', 'S833A.json','S833B.json', 'S833I1.json', 'S833I2.json', 'S833I3.json', 'S833I4.json'][:1]

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
                    key_words = dict()
                    for static_keyword, value in STATIC_KEYWORDS_MAPPING.items():
                        if static_keyword in text:
                            key_words[static_keyword] = value
                    for llm_keyword, value in LLM_KEYWORDS_MAPPING.items():
                        if llm_keyword in text:
                            key_words[llm_keyword] = value   

                    key_words = sorted(list(key_words.items()))[:5]
                    msg = f"原文:\n{text}\n\n关键词:\n"
                    for k, v in key_words:
                        if k.strip() and v.strip():
                            msg += f"{k}: {v}\n"
                    translated_text = chat_with_coze(BOT_ID_TRANSLATE, msg)

                    item[key] = translated_text
                    print(f"Translated {key} in {f}: {translated_text}")
                    print("="*100)

        cn_file_path = to_cn_path(kr_file_path)
        # print(cn_file_path, kr_data)
        write_file_data(cn_file_path, kr_data)
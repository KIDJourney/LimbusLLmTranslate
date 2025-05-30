from llm import ConversationBot, KEYWORDS_SP
import json
import os
from path import (
    KR_JSON_FILES,
    CN_JSON_FILES,
    KR_FILES_BASE_NAME_MAPPING,
    CN_FILES_BASE_NAME_MAPPING,
    read_file_data,
    AutoSavingDict
)
from keywords_const import STATIC_KEYWORDS_KEYS, LLM_KEYWORDS_MAPPING

MODIFY_KEYS = json.loads(open("./database/modify_keys.json", "r").read())

def handle_static_keywords():
    # 静态字段，直接映射
    cn_base_name_mapping = {os.path.basename(f): f for f in CN_JSON_FILES}
    kr_base_name_mapping = {os.path.basename(f): f for f in KR_JSON_FILES}
    common_files = set(cn_base_name_mapping.keys()) & set(kr_base_name_mapping.keys())

    static_key_words_mapping = AutoSavingDict('./database/keywords_static.json')

    for f in common_files:
        modify_key = MODIFY_KEYS.get(f)
        if not modify_key:
            print("No modify key for file: ", f)
            continue

        kr_file = kr_base_name_mapping[f]
        cn_file = cn_base_name_mapping[f]
        kr_json = json.loads(open(kr_file, "r").read())["dataList"]
        cn_json = json.loads(open(cn_file, "r").read())["dataList"]

        cn_item_mapping = {item.get("id"): item for item in cn_json}

        for kr_item in kr_json:
            kr_id = kr_item.get("id")
            if not kr_id:
                print("No kr id for item: ", kr_item)
                continue
            cn_item = cn_item_mapping.get(kr_id)
            if not cn_item:
                print("No cn item for kr item: ", kr_id)
                continue

            for key in STATIC_KEYWORDS_KEYS:
                kr_keyword = kr_item.get(key)
                cn_keyword = cn_item.get(key)

                if kr_keyword and cn_keyword:
                    static_key_words_mapping[kr_keyword] = str(cn_keyword)


def handle_llm_keywords():
    # 本次只处理S8的文件
    s8_file = [i for i in KR_FILES_BASE_NAME_MAPPING.keys() if i.startswith("8")]
    handle_files = sorted(list(set(s8_file) & set(CN_FILES_BASE_NAME_MAPPING.keys())))

    llm_keywords_mapping = AutoSavingDict("./database/keywords_llm.json")

    def handle_f(f):
        kr_file_path = KR_FILES_BASE_NAME_MAPPING[f]
        cn_file_path = CN_FILES_BASE_NAME_MAPPING[f]

        kr_data = read_file_data(kr_file_path)
        cn_data = read_file_data(cn_file_path)
        kr_item_map = {item.get("id"): item for item in kr_data if "id" in item}
        cn_item_map = {item.get("id"): item for item in cn_data if "id" in item}

        chat_bot = ConversationBot(KEYWORDS_SP)

        for kr_item in kr_data:
            kr_id = kr_item.get("id")
            if kr_id is None:
                print("No kr id for item: ", kr_item)
                continue
            cn_item = cn_item_map.get(kr_id)
            if not cn_item:
                print("No cn item for kr item: ", kr_id)
                continue

            kr_content = kr_item.get("content", "")
            cn_content = cn_item.get("content", "")

            if kr_content == cn_content:
                print(f"Content is the same for {kr_id} in {f}, skipping...")
                continue

            if kr_content and cn_content:
                msg = f"原文:\n{kr_content}\n\n译文:\n{cn_content}"
                keywords = chat_bot.chat(msg)
                print(f"Keywords for {kr_id} in {f}: {msg} {keywords}")

                try:
                    keywords = json.loads(keywords)
                except:
                    print(f"Error parsing keywords for {kr_id} in {f}: {keywords}")
                    continue

                print(f"Keywords for {kr_id} in {f}: {keywords}")

                for item in keywords:
                    if not item:
                        continue    
                    if len(item) != 2:
                        print(f"Invalid keyword item for {kr_id} in {f}: {item}")
                        continue
                    llm_keywords_mapping[item[0]] = item[1]

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(handle_f, handle_files))
    # for f in handle_files:
    #     print(f"Processing file: {f}")
    #     handle_f(f)

    llm_keywords_mapping._save()

if __name__ == "__main__":
    # handle_static_keywords()
    handle_llm_keywords()

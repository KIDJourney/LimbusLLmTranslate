import os 
import json 
import glob

BASE_PATH = "F:\SteamLibrary\steamapps\common\Limbus Company\LimbusCompany_Data"
KR_LANGE_PATH = os.path.join(BASE_PATH, "Assets\Resources_moved\Localize\kr")
CN_LANGE_PATH = os.path.join(BASE_PATH, "Lang\LLC_zh-CN")


def find_json_files(directory):
    pattern = os.path.join(directory, '**', '*.json')
    return glob.glob(pattern, recursive=True)


# kr is groundtruth, cn is modified
kr_json_files = find_json_files(KR_LANGE_PATH)
cn_json_files = find_json_files(CN_LANGE_PATH)
cn_name_map = {os.path.basename(f): f for f in cn_json_files}

changed_keys = {}

for kr_file in kr_json_files:
    print(f"Processing file: {kr_file}")
    kr_file_name = os.path.basename(kr_file)
    cn_file_name = kr_file_name.replace("KR_", "")

    if cn_file_name not in cn_name_map:
        print(f"Warning: Corresponding CN file not found for {kr_file_name}")
        continue
    cn_file_path = cn_name_map[cn_file_name]
    with open(kr_file, 'r', encoding='utf-8') as kr_f, open(cn_file_path, 'r', encoding='utf-8') as cn_f:
        kr_data = json.load(kr_f)['dataList']
        cn_data = json.load(cn_f)['dataList']

        kr_item_map = {item.get('id'):item for item in kr_data if 'id' in kr_data[0]}
        cn_item_map = {item.get('id'):item for item in cn_data if 'id' in cn_data[0]}

        modify_keys = set() 

        for id, item in kr_item_map.items():
            if id not in cn_item_map:
                print(f"Warning: ID {id} not found in CN file {cn_file_name}")
                continue
            
            kr_item = kr_item_map[id]
            cn_item = cn_item_map[id]


            for key in kr_item:
                if key in cn_item and kr_item[key] != cn_item[key]:
                    modify_keys.add(key)

    changed_keys[kr_file_name] = list(modify_keys)

with open("./database/modify_keys.json", 'w', encoding='utf-8') as f:
    json.dump(changed_keys, f, ensure_ascii=False, indent=4)

import os 
import json 
import glob

from path import BASE_PATH, KR_LANGE_PATH, CN_LANGE_PATH, KR_JSON_FILES, CN_JSON_FILES



if __name__ == "__main__":
# kr is groundtruth, cn is modified
    kr_json_files = KR_JSON_FILES
    cn_json_files = CN_JSON_FILES
    cn_name_map = {os.path.basename(f): f for f in cn_json_files}

    changed_keys = {}

    for kr_file_path in kr_json_files:
        kr_file_path_name = os.path.basename(kr_file_path)
        cn_file_name = kr_file_path_name.replace("KR_", "")

        if cn_file_name not in cn_name_map:
            print(f"Warning: Corresponding CN file not found for {kr_file_path_name}")
            continue
        cn_file_path = cn_name_map[cn_file_name]
        with open(kr_file_path, 'r', encoding='utf-8') as kr_f, open(cn_file_path, 'r', encoding='utf-8') as cn_f:
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

        # 保留最后两位的path作为key
        rel_path = os.path.basename(kr_file_path)
        dir_name = os.path.basename(os.path.dirname(kr_file_path))
        if dir_name != "kr":
            rel_path = os.path.join(dir_name, kr_file_path_name)
        
        changed_keys[rel_path] = sorted(list(modify_keys))

    with open("./database/modify_keys.json", 'w', encoding='utf-8') as f:
        json.dump(changed_keys, f, ensure_ascii=False, indent=4)

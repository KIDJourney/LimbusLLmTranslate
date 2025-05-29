import os
import glob
import json
import threading
import atexit

KR_LANGE_PATH = "./text_data/LocalizeLimbusCompany/KR"
CN_LANGE_PATH = "./text_data/LocalizeLimbusCompany/LLC_zh-CN"

def find_json_files(directory):
    pattern = os.path.join(directory, '**', '*.json')
    return glob.glob(pattern, recursive=True)

KR_JSON_FILES = find_json_files(KR_LANGE_PATH)
CN_JSON_FILES = find_json_files(CN_LANGE_PATH)

KR_FILES_BASE_NAME_MAPPING = {os.path.basename(f): f for f in KR_JSON_FILES}
CN_FILES_BASE_NAME_MAPPING = {os.path.basename(f): f for f in CN_JSON_FILES}


def read_file_data(file_path):
    """
    Reads the content of a file and returns it as a string.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.loads(f.read()).get('dataList', [])
    

def write_file_data(file_path, data):
    """
    Writes the content of a file and returns it as a string.
    """
    data = {
        "dataList": data
    }
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=4))



def to_cn_path(kr_path):
    sub_path = os.path.relpath(kr_path, KR_LANGE_PATH)
    return os.path.join(CN_LANGE_PATH, sub_path)


class AutoSavingDict(dict):
    def __init__(self, filepath):
        self._filepath = filepath
        self._lock = threading.Lock()

        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
        else:
            data = {}

        super().__init__(data)
        atexit.register(self._save)

    def __setitem__(self, key, value):
        with self._lock:
            super().__setitem__(key, value)

    def __delitem__(self, key):
        with self._lock:
            super().__delitem__(key)

    def update(self, *args, **kwargs):
        with self._lock:
            super().update(*args, **kwargs)

    def _save(self):
        with open(self._filepath, 'w', encoding='utf-8') as f:
            json.dump(self, f, indent=2, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    kr_base_name = "text_data/LocalizeLimbusCompany/KR/BgmLyrics/BgmLyrics_DistHeath.json"
    print(to_cn_path(kr_base_name))
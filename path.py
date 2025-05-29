import os
import glob

BASE_PATH = "F:\SteamLibrary\steamapps\common\Limbus Company\LimbusCompany_Data"
KR_LANGE_PATH = os.path.join(BASE_PATH, "Assets\Resources_moved\Localize\kr")
CN_LANGE_PATH = os.path.join(BASE_PATH, "Lang\LLC_zh-CN")

def find_json_files(directory):
    pattern = os.path.join(directory, '**', '*.json')
    return glob.glob(pattern, recursive=True)

KR_JSON_FILES = find_json_files(KR_LANGE_PATH)
CN_JSON_FILES = find_json_files(CN_LANGE_PATH)
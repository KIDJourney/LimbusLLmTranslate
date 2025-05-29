import os 
from path import KR_JSON_FILES, CN_JSON_FILES, KR_FILES_BASE_NAME_MAPPING, CN_FILES_BASE_NAME_MAPPING

# Find files that exist in KR_JSON_FILES but not in CN_JSON_FILES
kr_base_name = KR_FILES_BASE_NAME_MAPPING.keys()
cn_base_name = CN_FILES_BASE_NAME_MAPPING.keys()

new_files = set(kr_base_name) - set(cn_base_name)

# Print the new files
for file in sorted(new_files):
    print(f"{file}")

import os
import surya

surya_path = os.path.dirname(surya.__file__)
print(f"Surya path: {surya_path}")

for root, dirs, files in os.walk(surya_path):
    rel_root = os.path.relpath(root, surya_path)
    if rel_root == ".":
        print(f"Subdirs: {dirs}")
        print(f"Files: {files[:20]}")
    elif rel_root.count(os.sep) < 2:
        print(f"-- {rel_root}: {dirs}, {files[:5]}")

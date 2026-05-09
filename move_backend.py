import shutil

src = 'realtime_new.py'
dst = r'C:\MesugakiProto\dfogang_backend\api\routes\realtime.py'

try:
    shutil.copy(src, dst)
    print(f"Successfully copied {src} to {dst}")
except Exception as e:
    print(f"Error copying file: {e}")

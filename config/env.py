"""プロジェクト直下の .env ファイルを読み込み、環境変数として使えるようにする

外部ライブラリを使わず、次の書き方だけに対応したシンプルな読み込み処理:

    # コメント行
    KEY=VALUE
    KEY="VALUE"（前後の引用符は取り除く）

すでに同じ名前の環境変数が設定されている場合は、そちらを優先する（.env の値で上書きしない）。
"""
import os


def load_env_file(path):
    """path の .env ファイルを読み込んで os.environ に設定する。ファイルがなければ何もしない"""
    if not os.path.exists(path):
        return

    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue  # 空行・コメント行・形式が違う行は読み飛ばす
            key, value = line.split('=', 1)  # 最初の「=」で分ける（値の中の「=」はそのまま残す）
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)

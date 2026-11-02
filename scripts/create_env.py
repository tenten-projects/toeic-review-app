"""ローカル開発用の .env ファイルを作成するスクリプト

使い方（プロジェクトのフォルダで実行）:
    python scripts/create_env.py

ランダムな秘密鍵（DJANGO_SECRET_KEY）を新しく作り、プロジェクト直下に .env を保存する。
すでに .env がある場合は、大切な設定を消さないよう上書きせずに終了する。
"""
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / '.env'


def main():
    if ENV_PATH.exists():
        print(f'{ENV_PATH} はすでに存在するため、何もしませんでした。')
        return 1

    secret_key = secrets.token_urlsafe(50)  # 推測されにくいランダムな文字列（約67文字）
    ENV_PATH.write_text(
        '# ローカル開発用の設定ファイル\n'
        '# このファイルは Git で管理せず、他の人にも共有しないでください。\n'
        f'DJANGO_SECRET_KEY={secret_key}\n',
        encoding='utf-8',
    )
    print(f'{ENV_PATH} を作成しました。')
    return 0


if __name__ == '__main__':
    sys.exit(main())

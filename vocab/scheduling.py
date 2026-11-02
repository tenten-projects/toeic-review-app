"""復習スケジュール（間隔反復アルゴリズム）をまとめたモジュール

考え方:
    - 各単語は「復習レベル」(0〜5) と「次回復習予定日時」を持つ
    - 覚えていた   → レベルを1つ上げ、レベルに応じた日数だけ先に予定を延ばす
    - 覚えていなかった → レベル0に戻し、翌日に予定を入れる
    - 「今日の終わり（明日0:00）」より前に予定がある単語を、今日の復習対象とする

アルゴリズムを改良するときは、このファイルだけを変更すればよいようにしている。
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from .models import ReviewLog, Word

# 正解して到達したレベル → 次回復習までの日数
REVIEW_INTERVAL_DAYS = {
    1: 1,
    2: 3,
    3: 7,
    4: 14,
    5: 30,
}
MAX_LEVEL = max(REVIEW_INTERVAL_DAYS)  # レベルの上限（= 5）

# 不正解だったときに、次回復習まで空ける日数
INCORRECT_INTERVAL_DAYS = 1


def calculate_next_review(current_level, is_correct, reviewed_at):
    """復習結果から「新しいレベル」と「次回復習予定日時」を計算する

    データベースを使わない計算だけの関数なので、テストしやすい。

    例: レベル2の単語を正解 → (3, reviewed_at + 7日)
        レベル4の単語を不正解 → (0, reviewed_at + 1日)
    """
    if is_correct:
        new_level = min(current_level + 1, MAX_LEVEL)  # 上限を超えない
        days = REVIEW_INTERVAL_DAYS[new_level]
    else:
        new_level = 0
        days = INCORRECT_INTERVAL_DAYS
    return new_level, reviewed_at + timedelta(days=days)


def due_cutoff(now=None):
    """「今日の復習対象」の締め切り = 明日の0:00（settings.TIME_ZONE の日付で区切る）

    next_review_at がこの日時より前の単語は、今日の復習対象になる。
    データベースの日時は UTC で保存されているため、日本時間に直してから日付を求める。
    """
    if now is None:
        now = timezone.now()
    tomorrow = timezone.localtime(now).date() + timedelta(days=1)
    return timezone.make_aware(datetime.combine(tomorrow, time.min))


def due_words(now=None):
    """今日の復習対象の単語を、優先度の高い順に並べて返す

    並び順:
        1. 次回復習予定日時が古い順（長く待たされている単語ほど先）
        2. 不正解回数が多い順（苦手な単語ほど先）
        3. 復習レベルが低い順（まだ定着していない単語ほど先）
        4. ID順（ここまで同じでも、毎回同じ順番になるようにする）
    """
    return (
        Word.objects.with_stats()
        .filter(next_review_at__lt=due_cutoff(now))
        .order_by('next_review_at', '-incorrect_count', 'review_level', 'pk')
    )


def pick_next_word(now=None):
    """次に出題する単語を1つ選ぶ（今日の復習対象がなければ None）"""
    return due_words(now).first()


def record_review(word, is_correct, now=None):
    """復習結果を履歴に追加し、単語のレベルと次回復習予定日時を更新する

    履歴の追加と単語の更新は「両方成功」か「両方失敗」になるよう、
    transaction.atomic() でひとまとめにしている。
    """
    if now is None:
        now = timezone.now()

    with transaction.atomic():
        ReviewLog.objects.create(word=word, is_correct=is_correct, reviewed_at=now)
        word.review_level, word.next_review_at = calculate_next_review(word.review_level, is_correct, now)
        word.save(update_fields=['review_level', 'next_review_at'])
    return word

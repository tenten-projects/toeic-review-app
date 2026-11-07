"""復習スケジュール（間隔反復アルゴリズム）をまとめたモジュール

考え方:
    - 各単語は「復習レベル」(0〜5) と「次回復習予定日時」を持つ
    - 覚えていた   → レベルを1つ上げ、レベルに応じた日数だけ先に予定を延ばす
    - 覚えていなかった → レベル0に戻し、翌日に予定を入れる
    - 「今日の終わり（明日0:00）」より前に予定がある単語を、今日の復習対象とする

アルゴリズムを改良するときは、このファイルだけを変更すればよいようにしている。
"""
import random
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


def record_review(word, is_correct, now=None, mode=ReviewLog.MODE_TODAY):
    """復習結果を履歴に追加し、単語のレベルと次回復習予定日時を更新する

    履歴の追加と単語の更新は「両方成功」か「両方失敗」になるよう、
    transaction.atomic() でひとまとめにしている。
    mode は履歴に残す「復習の種類」（今日の復習 / ランダム復習）。計算方法は同じ。
    """
    if now is None:
        now = timezone.now()

    with transaction.atomic():
        ReviewLog.objects.create(word=word, is_correct=is_correct, reviewed_at=now, mode=mode)
        word.review_level, word.next_review_at = calculate_next_review(word.review_level, is_correct, now)
        word.save(update_fields=['review_level', 'next_review_at'])
    return word


# ---------------------------------------------------------------------------
# ランダム復習
# ---------------------------------------------------------------------------

# 復習レベル → 出題されやすさ（重み）。低いレベルほど出やすく、レベル5でも0にはしない
RANDOM_WEIGHTS = {0: 6, 1: 5, 2: 4, 3: 3, 4: 2, 5: 1}


def weighted_sample(items, weight_of, count=None, rng=random):
    """重み付きで、重複なしに items から count 個を選んで返す（count=None なら全部）

    1つ選ぶたびに、選んだものを候補から外して次を選ぶ（くじを箱に戻さない引き方）。
    そのため同じものは2回出ない。全部を選ぶ場合も「どれが選ばれるか」は変わらず、
    重みは「どれが先に出やすいか（順番）」にだけ影響する。

    rng: 乱数の出どころ。テストでは random.Random(固定の種) を渡して結果を再現できるようにする。
    """
    remaining = list(items)
    if count is None or count > len(remaining):
        count = len(remaining)

    chosen = []
    for _ in range(count):
        weights = [weight_of(item) for item in remaining]
        # 0以上「重みの合計」未満の数を1つ引き、それがどの区間に入るかで選ぶ
        point = rng.random() * sum(weights)
        for index, weight in enumerate(weights):
            point -= weight
            if point < 0:
                break
        chosen.append(remaining.pop(index))
    return chosen


def pick_random_words(levels, count=None, rng=random):
    """ランダム復習で出題する単語のIDを、出題する順に並べて返す

    levels: 対象にする復習レベルのリスト（例: [0, 1, 2]）
    count:  問題数（None なら対象の全単語）。対象がそれより少なければ、ある分だけ返す
    """
    candidates = list(Word.objects.filter(review_level__in=levels).order_by('pk').values_list('pk', 'review_level'))
    picked = weighted_sample(candidates, lambda c: RANDOM_WEIGHTS[c[1]], count, rng)
    return [pk for pk, _level in picked]


def record_random_review(word, is_correct, now=None):
    """ランダム復習の結果を記録する（案B）

    - 今日の復習対象の単語            → 今日の復習と同じルールで更新する
    - 予定日より前の単語で「覚えていた」 → 履歴だけ残し、レベルと次回復習日は変えない
      （予定より早く思い出せただけなので、間隔を延ばす根拠としては弱いため）
    - 予定日より前の単語で「覚えていなかった」→ 今日の復習と同じくレベル0・翌日に戻す
      （忘れていることが分かったので、予定を待たずに復習し直す）

    戻り値: レベルと次回復習日を更新したら True、履歴だけ残したら False
    """
    if now is None:
        now = timezone.now()

    is_due = word.next_review_at < due_cutoff(now)
    if is_due or not is_correct:
        record_review(word, is_correct, now, mode=ReviewLog.MODE_RANDOM)
        return True

    ReviewLog.objects.create(word=word, is_correct=True, reviewed_at=now, mode=ReviewLog.MODE_RANDOM)
    return False

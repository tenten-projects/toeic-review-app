"""ダッシュボードに表示する数値やグラフ用のデータを集計するモジュール

どの関数も「現在日時 now」を引数で受け取るので、テストでは日時を固定して確かめられる。
「今日」「〇日」は settings.TIME_ZONE（Asia/Tokyo）の日付で区切る。
"""
from datetime import datetime, time, timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from . import scheduling
from .models import ReviewLog, Word

CHART_DAYS = 7  # 日別グラフに表示する日数（今日を含む）


def local_day_start(day):
    """日付 day の 0:00（日本時間）を、タイムゾーン付きの日時で返す"""
    return timezone.make_aware(datetime.combine(day, time.min))


def calc_accuracy(correct, total):
    """正答率（0〜100）を返す。復習回数が0なら、0で割らずに None を返す"""
    if total == 0:
        return None
    return correct * 100 / total


def count_reviews(logs):
    """ReviewLog の集まりから {'total': 復習回数, 'correct': 正解回数} を1回のクエリで数える"""
    return logs.aggregate(
        total=Count('id'),
        correct=Count('id', filter=Q(is_correct=True)),
    )


def get_summary(now):
    """サマリーカードに表示する数値をまとめて返す"""
    today = timezone.localtime(now).date()  # UTC → 日本時間に直してから日付を取り出す
    today_logs = ReviewLog.objects.filter(
        reviewed_at__gte=local_day_start(today),                      # 今日の0:00以降
        reviewed_at__lt=local_day_start(today + timedelta(days=1)),   # 明日の0:00より前
    )
    today_counts = count_reviews(today_logs)

    return {
        'total_words': Word.objects.count(),
        # 復習画面と同じ関数を使うので、件数が必ず一致する
        'due_count': scheduling.due_words(now).count(),
        'today_reviews': today_counts['total'],
        'today_correct': today_counts['correct'],
        'today_accuracy': calc_accuracy(today_counts['correct'], today_counts['total']),
        'total_reviews': ReviewLog.objects.count(),
    }


def get_daily_reviews(now, days=CHART_DAYS):
    """過去 days 日間（今日を含む）の日別の復習回数を、古い日から順にリストで返す

    復習しなかった日も total=0 として含める。
    height は棒グラフの高さ（一番多い日を100とした割合）。
    """
    today = timezone.localtime(now).date()
    first_day = today - timedelta(days=days - 1)

    # 期間内の履歴を「日本時間の日付」ごとにまとめて数える（クエリは1回）
    rows = (
        ReviewLog.objects
        .filter(
            reviewed_at__gte=local_day_start(first_day),
            reviewed_at__lt=local_day_start(today + timedelta(days=1)),
        )
        .annotate(day=TruncDate('reviewed_at'))  # 日時 → 日付（現在のタイムゾーン = 日本時間で）
        .values('day')                            # 日付ごとにグループ化
        .annotate(total=Count('id'), correct=Count('id', filter=Q(is_correct=True)))
        .order_by('day')
    )
    counts_by_day = {row['day']: row for row in rows}
    max_total = max((row['total'] for row in counts_by_day.values()), default=0)

    daily = []
    for i in range(days):
        day = first_day + timedelta(days=i)
        row = counts_by_day.get(day)  # その日の履歴がなければ None
        total = row['total'] if row else 0
        correct = row['correct'] if row else 0
        daily.append({
            'date': day,
            'is_today': day == today,
            'total': total,
            'correct': correct,
            'incorrect': total - correct,
            'height': round(total * 100 / max_total) if max_total else 0,
        })
    return daily


def get_level_counts():
    """復習レベル0〜上限ごとの単語数をリストで返す（0語のレベルも含める）

    percent は全単語に対する割合（棒グラフの長さに使う）。
    interval_days はそのレベルで正解した直後に空く日数（レベル0は None）。
    """
    rows = Word.objects.values('review_level').annotate(count=Count('id')).order_by('review_level')
    counts = {row['review_level']: row['count'] for row in rows}
    total = sum(counts.values())

    levels = []
    for level in range(scheduling.MAX_LEVEL + 1):
        count = counts.get(level, 0)
        levels.append({
            'level': level,
            'count': count,
            'percent': count * 100 / total if total else 0,
            'interval_days': scheduling.REVIEW_INTERVAL_DAYS.get(level),
        })
    return levels

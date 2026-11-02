import os
import tempfile
from datetime import datetime, timedelta
from html.parser import HTMLParser
from importlib import import_module
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from config.env import load_env_file
from django.apps import apps
from django.contrib.staticfiles import finders
from django.template.loader import get_template
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import dashboard as dashboard_stats
from . import scheduling
from .models import ReviewLog, Word

JST = ZoneInfo('Asia/Tokyo')
# テストで使う「現在日時」。本物の現在日時に左右されないよう固定する
FIXED_NOW = datetime(2026, 4, 1, 10, 0, tzinfo=JST)


def freeze_time(dt):
    """with freeze_time(...): の中では timezone.now() が dt を返すようにする"""
    return mock.patch('django.utils.timezone.now', return_value=dt)


class WordListViewTests(TestCase):
    """一覧表示と登録のテスト"""

    def test_empty_list(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'まだ単語が登録されていません。')

    def test_list_shows_words(self):
        Word.objects.create(english='negotiate', meaning='交渉する')
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, 'negotiate')
        self.assertContains(response, '交渉する')

    def test_create_word(self):
        response = self.client.post(reverse('vocab:word_list'), {
            'english': 'invoice',
            'meaning': '請求書',
            'example': 'Please send the invoice.',
        })
        self.assertRedirects(response, reverse('vocab:word_list'))
        self.assertEqual(Word.objects.count(), 1)
        word = Word.objects.get()
        self.assertEqual(word.english, 'invoice')
        self.assertEqual(word.example, 'Please send the invoice.')

    def test_create_word_without_example(self):
        self.client.post(reverse('vocab:word_list'), {'english': 'deadline', 'meaning': '締め切り'})
        self.assertEqual(Word.objects.count(), 1)

    def test_create_word_missing_required_field(self):
        response = self.client.post(reverse('vocab:word_list'), {'english': '', 'meaning': '請求書'})
        self.assertEqual(response.status_code, 200)  # エラー表示のため同じページに戻る
        self.assertEqual(Word.objects.count(), 0)
        self.assertTrue(response.context['form'].errors)


class WordDeleteViewTests(TestCase):
    """削除のテスト"""

    def setUp(self):
        self.word = Word.objects.create(english='postpone', meaning='延期する')
        self.url = reverse('vocab:word_delete', args=[self.word.pk])

    def test_get_shows_confirmation(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '削除しますか')
        self.assertEqual(Word.objects.count(), 1)  # GETでは削除されない

    def test_post_deletes_word(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse('vocab:word_list'))
        self.assertFalse(Word.objects.filter(pk=self.word.pk).exists())

    def test_delete_nonexistent_word_returns_404(self):
        response = self.client.post(reverse('vocab:word_delete', args=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_delete_word_also_deletes_review_logs(self):
        ReviewLog.objects.create(word=self.word, is_correct=True)
        self.client.post(self.url)
        self.assertEqual(ReviewLog.objects.count(), 0)


class ReviewViewTests(TestCase):
    """出題ページのテスト"""

    def test_no_words_shows_guidance(self):
        response = self.client.get(reverse('vocab:review'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '復習できる単語がありません。')

    def test_shows_word_and_hidden_answer(self):
        Word.objects.create(english='invoice', meaning='請求書', example='Please send the invoice.')
        response = self.client.get(reverse('vocab:review'))
        self.assertContains(response, 'invoice')
        self.assertContains(response, '答えを見る')
        self.assertContains(response, '<details>')  # 意味は <details> の中に隠れている
        self.assertContains(response, '請求書')
        self.assertContains(response, 'Please send the invoice.')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_get_does_not_create_review_log(self):
        Word.objects.create(english='invoice', meaning='請求書')
        self.client.get(reverse('vocab:review'))
        self.client.get(reverse('vocab:review'))  # 再読み込み
        self.assertEqual(ReviewLog.objects.count(), 0)


class ReviewAnswerViewTests(TestCase):
    """復習結果の保存のテスト"""

    def setUp(self):
        self.word = Word.objects.create(english='negotiate', meaning='交渉する')
        self.url = reverse('vocab:review_answer', args=[self.word.pk])

    def test_post_correct_saves_log(self):
        response = self.client.post(self.url, {'result': 'correct'})
        self.assertRedirects(response, reverse('vocab:review'))
        log = ReviewLog.objects.get()
        self.assertEqual(log.word, self.word)
        self.assertTrue(log.is_correct)
        self.assertIsNotNone(log.reviewed_at)

    def test_post_incorrect_saves_log(self):
        self.client.post(self.url, {'result': 'incorrect'})
        self.assertFalse(ReviewLog.objects.get().is_correct)

    def test_logs_accumulate(self):
        self.client.post(self.url, {'result': 'correct'})
        self.client.post(self.url, {'result': 'incorrect'})
        self.client.post(self.url, {'result': 'correct'})
        self.assertEqual(self.word.reviews.count(), 3)

    def test_get_is_not_allowed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(ReviewLog.objects.count(), 0)

    def test_invalid_result_returns_400(self):
        response = self.client.post(self.url, {'result': 'maybe'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ReviewLog.objects.count(), 0)

    def test_nonexistent_word_returns_404(self):
        response = self.client.post(reverse('vocab:review_answer', args=[9999]), {'result': 'correct'})
        self.assertEqual(response.status_code, 404)

    def test_post_without_csrf_token_is_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)  # 本番と同じくCSRFチェックを有効にする
        response = csrf_client.post(self.url, {'result': 'correct'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ReviewLog.objects.count(), 0)


class WordStatsTests(TestCase):
    """復習成績の集計と一覧表示のテスト"""

    def setUp(self):
        self.word = Word.objects.create(english='postpone', meaning='延期する')

    def test_unreviewed_word_stats(self):
        word = Word.objects.with_stats().get(pk=self.word.pk)
        self.assertEqual(word.review_count, 0)
        self.assertEqual(word.correct_count, 0)
        self.assertEqual(word.incorrect_count, 0)
        self.assertIsNone(word.accuracy)
        self.assertIsNone(word.last_reviewed_at)

    def test_reviewed_word_stats(self):
        now = timezone.now()
        ReviewLog.objects.create(word=self.word, is_correct=True, reviewed_at=now - timedelta(days=2))
        ReviewLog.objects.create(word=self.word, is_correct=False, reviewed_at=now)
        ReviewLog.objects.create(word=self.word, is_correct=True, reviewed_at=now - timedelta(days=1))

        word = Word.objects.with_stats().get(pk=self.word.pk)
        self.assertEqual(word.review_count, 3)
        self.assertEqual(word.correct_count, 2)
        self.assertEqual(word.incorrect_count, 1)
        self.assertAlmostEqual(word.accuracy, 200 / 3)
        self.assertEqual(word.last_reviewed_at, now)

    def test_stats_are_per_word(self):
        other = Word.objects.create(english='invoice', meaning='請求書')
        ReviewLog.objects.create(word=other, is_correct=True)
        word = Word.objects.with_stats().get(pk=self.word.pk)
        self.assertEqual(word.review_count, 0)

    def test_list_shows_unreviewed(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '未復習')

    def test_list_shows_stats(self):
        ReviewLog.objects.create(word=self.word, is_correct=True)
        ReviewLog.objects.create(word=self.word, is_correct=False)
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '復習2回')
        self.assertContains(response, '正解1回')
        self.assertContains(response, '不正解1回')
        self.assertContains(response, '正答率50%')
        self.assertNotContains(response, '未復習')


class CalculateNextReviewTests(TestCase):
    """次回復習の計算（データベースを使わない純粋な計算）のテスト"""

    def test_correct_raises_level_and_sets_interval(self):
        # (今のレベル, 正解後のレベル, 次回までの日数)
        cases = [(0, 1, 1), (1, 2, 3), (2, 3, 7), (3, 4, 14), (4, 5, 30)]
        for current, expected_level, expected_days in cases:
            with self.subTest(current_level=current):
                level, next_at = scheduling.calculate_next_review(current, True, FIXED_NOW)
                self.assertEqual(level, expected_level)
                self.assertEqual(next_at, FIXED_NOW + timedelta(days=expected_days))

    def test_level_does_not_exceed_max(self):
        level, next_at = scheduling.calculate_next_review(5, True, FIXED_NOW)
        self.assertEqual(level, 5)
        self.assertEqual(next_at, FIXED_NOW + timedelta(days=30))

    def test_incorrect_resets_level_to_zero_and_next_day(self):
        for current in range(6):
            with self.subTest(current_level=current):
                level, next_at = scheduling.calculate_next_review(current, False, FIXED_NOW)
                self.assertEqual(level, 0)
                self.assertEqual(next_at, FIXED_NOW + timedelta(days=1))


class DueCutoffTests(TestCase):
    """「今日の復習対象」の締め切り（明日0:00 日本時間）のテスト"""

    def test_cutoff_is_start_of_tomorrow_in_local_time(self):
        self.assertEqual(scheduling.due_cutoff(FIXED_NOW), datetime(2026, 4, 2, 0, 0, tzinfo=JST))

    def test_cutoff_uses_japan_date_not_utc_date(self):
        # 日本時間 4/1 23:30 は UTC では 4/1 14:30。日本時間で日付を区切ること
        late_night = datetime(2026, 4, 1, 23, 30, tzinfo=JST)
        self.assertEqual(scheduling.due_cutoff(late_night), datetime(2026, 4, 2, 0, 0, tzinfo=JST))
        # 日本時間 4/2 0:30 は UTC ではまだ 4/1 15:30。それでも「今日」は 4/2
        after_midnight = datetime(2026, 4, 2, 0, 30, tzinfo=JST)
        self.assertEqual(scheduling.due_cutoff(after_midnight), datetime(2026, 4, 3, 0, 0, tzinfo=JST))


class DueWordsTests(TestCase):
    """今日の復習対象の判定と、出題の優先順位のテスト"""

    def make_word(self, english, next_review_at, level=0):
        return Word.objects.create(english=english, meaning='意味', review_level=level, next_review_at=next_review_at)

    def due_list(self):
        return list(scheduling.due_words(FIXED_NOW))

    def test_new_word_is_due_immediately(self):
        with freeze_time(FIXED_NOW):
            word = Word.objects.create(english='invoice', meaning='請求書')
        self.assertEqual(word.review_level, 0)
        self.assertEqual(word.next_review_at, FIXED_NOW)
        self.assertEqual(scheduling.pick_next_word(FIXED_NOW), word)

    def test_boundary_conditions(self):
        cutoff = datetime(2026, 4, 2, 0, 0, tzinfo=JST)
        cases = [
            ('ちょうど今', FIXED_NOW, True),
            ('今日の23:59:59', cutoff - timedelta(seconds=1), True),
            ('明日0:00ちょうど', cutoff, False),
            ('明日の昼', cutoff + timedelta(hours=12), False),
        ]
        for label, next_review_at, expected in cases:
            with self.subTest(label):
                Word.objects.all().delete()
                word = self.make_word('word', next_review_at)
                self.assertEqual(word in self.due_list(), expected)

    def test_future_words_are_not_picked(self):
        self.make_word('future', FIXED_NOW + timedelta(days=3))
        self.assertIsNone(scheduling.pick_next_word(FIXED_NOW))

    def test_older_due_date_comes_first(self):
        newer = self.make_word('newer', FIXED_NOW - timedelta(days=1))
        older = self.make_word('older', FIXED_NOW - timedelta(days=5))
        self.make_word('future', FIXED_NOW + timedelta(days=2))
        self.assertEqual(self.due_list(), [older, newer])

    def test_same_due_date_more_incorrect_first(self):
        due = FIXED_NOW - timedelta(days=1)
        few = self.make_word('few_mistakes', due)
        many = self.make_word('many_mistakes', due)
        ReviewLog.objects.create(word=few, is_correct=False, reviewed_at=FIXED_NOW - timedelta(days=10))
        for days in (10, 9, 8):
            ReviewLog.objects.create(word=many, is_correct=False, reviewed_at=FIXED_NOW - timedelta(days=days))
        self.assertEqual(self.due_list(), [many, few])

    def test_same_due_date_and_mistakes_lower_level_first(self):
        due = FIXED_NOW - timedelta(days=1)
        high = self.make_word('high', due, level=3)
        low = self.make_word('low', due, level=1)
        self.assertEqual(self.due_list(), [low, high])

    def test_complete_tie_is_stable(self):
        due = FIXED_NOW - timedelta(days=1)
        first = self.make_word('first', due)
        second = self.make_word('second', due)
        for _ in range(5):  # 何度呼んでも同じ順番（ID順）になる
            self.assertEqual(self.due_list(), [first, second])


class RecordReviewTests(TestCase):
    """復習結果の記録と、レベル・次回予定の更新のテスト"""

    def setUp(self):
        self.word = Word.objects.create(english='negotiate', meaning='交渉する', next_review_at=FIXED_NOW)

    def refresh(self):
        self.word.refresh_from_db()
        return self.word

    def test_correct_raises_level_and_sets_next_review(self):
        scheduling.record_review(self.word, True, FIXED_NOW)
        word = self.refresh()
        self.assertEqual(word.review_level, 1)
        self.assertEqual(word.next_review_at, FIXED_NOW + timedelta(days=1))

    def test_repeated_correct_follows_interval_table(self):
        now = FIXED_NOW
        for expected_level, expected_days in [(1, 1), (2, 3), (3, 7), (4, 14), (5, 30), (5, 30)]:
            scheduling.record_review(self.word, True, now)
            word = self.refresh()
            self.assertEqual(word.review_level, expected_level)
            self.assertEqual(word.next_review_at, now + timedelta(days=expected_days))
            now = word.next_review_at  # 予定日ちょうどに次の復習をしたとする

    def test_incorrect_resets_level_and_sets_next_day(self):
        self.word.review_level = 4
        self.word.save()
        scheduling.record_review(self.word, False, FIXED_NOW)
        word = self.refresh()
        self.assertEqual(word.review_level, 0)
        self.assertEqual(word.next_review_at, FIXED_NOW + timedelta(days=1))

    def test_answered_word_is_not_due_again_today(self):
        for is_correct in (True, False):
            with self.subTest(is_correct=is_correct):
                self.word.next_review_at = FIXED_NOW
                self.word.save()
                scheduling.record_review(self.word, is_correct, FIXED_NOW)
                self.assertIsNone(scheduling.pick_next_word(FIXED_NOW))

    def test_existing_history_is_kept(self):
        old1 = ReviewLog.objects.create(word=self.word, is_correct=True, reviewed_at=FIXED_NOW - timedelta(days=5))
        old2 = ReviewLog.objects.create(word=self.word, is_correct=False, reviewed_at=FIXED_NOW - timedelta(days=2))

        scheduling.record_review(self.word, True, FIXED_NOW)

        self.assertEqual(self.word.reviews.count(), 3)
        old1.refresh_from_db()
        old2.refresh_from_db()
        self.assertTrue(old1.is_correct)
        self.assertEqual(old1.reviewed_at, FIXED_NOW - timedelta(days=5))
        self.assertFalse(old2.is_correct)
        newest = self.word.reviews.first()  # ordering は新しい順
        self.assertTrue(newest.is_correct)
        self.assertEqual(newest.reviewed_at, FIXED_NOW)


class ScheduledReviewViewTests(TestCase):
    """画面（ビュー）経由での復習スケジュールのテスト。現在日時を固定して検証する"""

    def test_registered_word_appears_in_review(self):
        with freeze_time(FIXED_NOW):
            self.client.post(reverse('vocab:word_list'), {'english': 'invoice', 'meaning': '請求書'})
            response = self.client.get(reverse('vocab:review'))
        self.assertEqual(response.context['word'].english, 'invoice')
        self.assertContains(response, '本日の復習対象: <strong>1件</strong>', html=True)

    def test_due_word_is_prioritized_over_future_word(self):
        Word.objects.create(english='future', meaning='未来', next_review_at=FIXED_NOW + timedelta(days=3))
        Word.objects.create(english='overdue', meaning='期限切れ', next_review_at=FIXED_NOW - timedelta(days=2))
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:review'))
        self.assertEqual(response.context['word'].english, 'overdue')
        self.assertEqual(response.context['due_count'], 1)

    def test_no_due_words_shows_message(self):
        Word.objects.create(english='future', meaning='未来', next_review_at=FIXED_NOW + timedelta(days=3))
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:review'))
        self.assertIsNone(response.context['word'])
        self.assertContains(response, '本日復習すべき単語はありません。')
        self.assertContains(response, '本日の復習対象: <strong>0件</strong>', html=True)
        self.assertContains(response, '2026/04/04')  # 次の復習予定を案内する
        self.assertNotContains(response, '答えを見る')

    def test_answer_updates_schedule_and_redirects(self):
        word = Word.objects.create(english='invoice', meaning='請求書', review_level=2, next_review_at=FIXED_NOW)
        with freeze_time(FIXED_NOW):
            response = self.client.post(reverse('vocab:review_answer', args=[word.pk]), {'result': 'correct'})
        self.assertRedirects(response, reverse('vocab:review'), fetch_redirect_response=False)
        word.refresh_from_db()
        self.assertEqual(word.review_level, 3)
        self.assertEqual(word.next_review_at, FIXED_NOW + timedelta(days=7))
        self.assertEqual(ReviewLog.objects.get().reviewed_at, FIXED_NOW)

    def test_word_list_shows_schedule(self):
        Word.objects.create(english='today', meaning='今日', review_level=0, next_review_at=FIXED_NOW)
        Word.objects.create(english='later', meaning='後で', review_level=3, next_review_at=FIXED_NOW + timedelta(days=7))
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '復習レベル 0')
        self.assertContains(response, '復習レベル 3')
        self.assertContains(response, '次回復習予定: 2026/04/01')
        self.assertContains(response, '次回復習予定: 2026/04/08')
        self.assertContains(response, '今日の復習対象', count=1)  # 'today' だけに付く


class RebuildScheduleMigrationTests(TestCase):
    """既存データ用マイグレーション（履歴の再生）のテスト"""

    def run_migration_function(self):
        migration = import_module('vocab.migrations.0004_rebuild_review_schedule')
        migration.rebuild_review_schedule(apps, None)

    def test_rebuilds_from_history_and_keeps_logs(self):
        reviewed = Word.objects.create(english='reviewed', meaning='復習済み')
        unreviewed = Word.objects.create(english='unreviewed', meaning='未復習')
        t = FIXED_NOW
        # 正解 → 正解 → 不正解 → 正解 の順に復習していた（わざとバラバラの順で作る）
        ReviewLog.objects.create(word=reviewed, is_correct=True, reviewed_at=t + timedelta(days=3))
        ReviewLog.objects.create(word=reviewed, is_correct=True, reviewed_at=t)
        ReviewLog.objects.create(word=reviewed, is_correct=False, reviewed_at=t + timedelta(days=2))
        ReviewLog.objects.create(word=reviewed, is_correct=True, reviewed_at=t + timedelta(days=1))

        self.run_migration_function()

        reviewed.refresh_from_db()
        unreviewed.refresh_from_db()
        # 最後の不正解でレベル0 → 最後の正解でレベル1、次回はその1日後
        self.assertEqual(reviewed.review_level, 1)
        self.assertEqual(reviewed.next_review_at, t + timedelta(days=4))
        # 未復習の単語は登録時刻（= すぐに復習対象）
        self.assertEqual(unreviewed.review_level, 0)
        self.assertEqual(unreviewed.next_review_at, unreviewed.created_at)
        self.assertEqual(ReviewLog.objects.count(), 4)  # 履歴は消えない


def jst(month, day, hour=0, minute=0, second=0):
    """2026年の日本時間の日時を作る（テストを読みやすくするため）"""
    return datetime(2026, month, day, hour, minute, second, tzinfo=JST)


class DashboardSummaryTests(TestCase):
    """サマリー（カードの数値）の集計のテスト。FIXED_NOW = 2026/4/1 10:00（日本時間）"""

    def setUp(self):
        self.word = Word.objects.create(english='invoice', meaning='請求書', next_review_at=FIXED_NOW + timedelta(days=1))

    def log(self, is_correct, reviewed_at):
        ReviewLog.objects.create(word=self.word, is_correct=is_correct, reviewed_at=reviewed_at)

    def test_no_reviews(self):
        summary = dashboard_stats.get_summary(FIXED_NOW)
        self.assertEqual(summary['total_words'], 1)
        self.assertEqual(summary['today_reviews'], 0)
        self.assertEqual(summary['today_correct'], 0)
        self.assertIsNone(summary['today_accuracy'])  # 0で割らずに None
        self.assertEqual(summary['total_reviews'], 0)

    def test_total_words(self):
        Word.objects.create(english='deadline', meaning='締め切り')
        Word.objects.create(english='postpone', meaning='延期する')
        self.assertEqual(dashboard_stats.get_summary(FIXED_NOW)['total_words'], 3)

    def test_today_reviews_correct_and_accuracy(self):
        self.log(True, jst(4, 1, 0, 0, 0))     # 今日の0:00ちょうど → 今日
        self.log(True, jst(4, 1, 9, 0))
        self.log(False, jst(4, 1, 9, 30))
        self.log(False, jst(3, 31, 23, 59, 59))  # 昨日の最後の1秒 → 今日ではない

        summary = dashboard_stats.get_summary(FIXED_NOW)
        self.assertEqual(summary['today_reviews'], 3)
        self.assertEqual(summary['today_correct'], 2)
        self.assertAlmostEqual(summary['today_accuracy'], 200 / 3)  # 2 ÷ 3 × 100
        self.assertEqual(summary['total_reviews'], 4)  # 累計は昨日の分も含む

    def test_today_uses_japan_date(self):
        # 日本時間 4/2 0:30 は、UTC ではまだ 4/1 15:30
        now = jst(4, 2, 0, 30)
        self.log(True, jst(4, 1, 23, 50))   # UTC では now と同じ 4/1 だが、日本時間では昨日
        self.log(False, jst(4, 2, 0, 10))   # 日本時間で今日
        summary = dashboard_stats.get_summary(now)
        self.assertEqual(summary['today_reviews'], 1)
        self.assertEqual(summary['today_correct'], 0)
        self.assertEqual(summary['today_accuracy'], 0)

    def test_due_count_matches_review_page(self):
        Word.objects.create(english='overdue', meaning='期限切れ', next_review_at=FIXED_NOW - timedelta(days=2))
        Word.objects.create(english='tonight', meaning='今夜', next_review_at=jst(4, 1, 22, 0))
        Word.objects.create(english='tomorrow', meaning='明日', next_review_at=jst(4, 2, 0, 0))
        with freeze_time(FIXED_NOW):
            dashboard_response = self.client.get(reverse('vocab:dashboard'))
            review_response = self.client.get(reverse('vocab:review'))
        self.assertEqual(dashboard_response.context['summary']['due_count'], 2)
        self.assertEqual(dashboard_response.context['summary']['due_count'], review_response.context['due_count'])


class DailyReviewsTests(TestCase):
    """過去7日間の日別集計のテスト"""

    def setUp(self):
        self.word = Word.objects.create(english='invoice', meaning='請求書')

    def log(self, is_correct, reviewed_at):
        ReviewLog.objects.create(word=self.word, is_correct=is_correct, reviewed_at=reviewed_at)

    def test_seven_days_including_days_without_reviews(self):
        daily = dashboard_stats.get_daily_reviews(FIXED_NOW)
        self.assertEqual([d['date'] for d in daily], [jst(3, 26 + i).date() for i in range(6)] + [jst(4, 1).date()])
        self.assertTrue(all(d['total'] == 0 and d['height'] == 0 for d in daily))
        self.assertEqual([d['is_today'] for d in daily], [False] * 6 + [True])

    def test_daily_counts(self):
        self.log(True, jst(4, 1, 8, 0))
        self.log(False, jst(4, 1, 9, 0))
        self.log(True, jst(3, 30, 12, 0))
        self.log(False, jst(3, 26, 0, 0))         # 7日前の最初の日（0:00ちょうど）→ 含む
        self.log(True, jst(3, 25, 23, 59, 59))    # 8日前 → 含まない

        daily = dashboard_stats.get_daily_reviews(FIXED_NOW)
        self.assertEqual([d['total'] for d in daily], [1, 0, 0, 0, 1, 0, 2])
        self.assertEqual([d['correct'] for d in daily], [0, 0, 0, 0, 1, 0, 1])
        self.assertEqual([d['incorrect'] for d in daily], [1, 0, 0, 0, 0, 0, 1])
        self.assertEqual([d['height'] for d in daily], [50, 0, 0, 0, 50, 0, 100])  # 最大の日 = 100

    def test_groups_by_japan_date(self):
        self.log(True, jst(3, 30, 23, 30))  # UTC 3/30 14:30
        self.log(True, jst(3, 31, 0, 30))   # UTC ではまだ 3/30 15:30 だが、日本時間では 3/31
        totals = {d['date']: d['total'] for d in dashboard_stats.get_daily_reviews(FIXED_NOW)}
        self.assertEqual(totals[jst(3, 30).date()], 1)
        self.assertEqual(totals[jst(3, 31).date()], 1)


class LevelCountsTests(TestCase):
    """復習レベル別の単語数のテスト"""

    def test_counts_each_level(self):
        for i, level in enumerate([0, 0, 2, 5]):
            Word.objects.create(english=f'word{i}', meaning='意味', review_level=level)
        levels = dashboard_stats.get_level_counts()
        self.assertEqual([item['level'] for item in levels], [0, 1, 2, 3, 4, 5])
        self.assertEqual([item['count'] for item in levels], [2, 0, 1, 0, 0, 1])
        self.assertEqual(levels[0]['percent'], 50)
        self.assertEqual(levels[2]['percent'], 25)
        self.assertEqual([item['interval_days'] for item in levels], [None, 1, 3, 7, 14, 30])

    def test_no_words(self):
        levels = dashboard_stats.get_level_counts()
        self.assertEqual(len(levels), 6)
        self.assertTrue(all(item['count'] == 0 and item['percent'] == 0 for item in levels))


class DashboardViewTests(TestCase):
    """ダッシュボード画面の表示と、既存機能との連携のテスト"""

    def get_dashboard(self):
        with freeze_time(FIXED_NOW):
            return self.client.get(reverse('vocab:dashboard'))

    def test_empty_dashboard(self):
        response = self.get_dashboard()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'まだ復習していません')
        self.assertContains(response, 'class="bar-col is-empty', count=7)  # 7日とも「復習しなかった日」
        self.assertContains(response, 'レベル5')
        self.assertContains(response, '0<small>語</small>', count=2)  # 登録単語数・今日の残り復習
        self.assertContains(response, '<span class="hbar-value">0語</span>', count=6)  # レベル0〜5

    def test_dashboard_shows_numbers(self):
        word = Word.objects.create(english='invoice', meaning='請求書')
        ReviewLog.objects.create(word=word, is_correct=True, reviewed_at=jst(4, 1, 8, 0))
        ReviewLog.objects.create(word=word, is_correct=True, reviewed_at=jst(4, 1, 8, 30))
        ReviewLog.objects.create(word=word, is_correct=False, reviewed_at=jst(4, 1, 9, 0))
        response = self.get_dashboard()
        self.assertContains(response, '67<small>%</small>')  # 2 ÷ 3 × 100 ≒ 67
        self.assertContains(response, '○2 ×1')
        self.assertContains(response, 'class="bar-col is-empty', count=6)  # 今日以外の6日
        self.assertNotContains(response, 'まだ復習していません')

    def test_navigation_has_dashboard_link(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, f'href="{reverse("vocab:dashboard")}"')

    def test_register_review_delete_flow(self):
        """登録 → 復習 → 削除 の一連の操作が、ダッシュボードに正しく反映される"""
        with freeze_time(FIXED_NOW):
            self.client.post(reverse('vocab:word_list'), {'english': 'invoice', 'meaning': '請求書'})
            word = Word.objects.get()
            summary = self.client.get(reverse('vocab:dashboard')).context['summary']
            self.assertEqual(summary['total_words'], 1)
            self.assertEqual(summary['due_count'], 1)

            self.client.post(reverse('vocab:review_answer', args=[word.pk]), {'result': 'correct'})
            response = self.client.get(reverse('vocab:dashboard'))
            summary = response.context['summary']
            self.assertEqual(summary['due_count'], 0)
            self.assertEqual(summary['today_reviews'], 1)
            self.assertEqual(summary['today_correct'], 1)
            self.assertEqual(summary['today_accuracy'], 100)
            self.assertEqual(response.context['level_counts'][1]['count'], 1)  # レベル1に1語

            self.client.post(reverse('vocab:word_delete', args=[word.pk]))
            summary = self.client.get(reverse('vocab:dashboard')).context['summary']
            self.assertEqual(summary['total_words'], 0)
            self.assertEqual(summary['total_reviews'], 0)  # 単語と一緒に履歴も削除される（既存の仕様）


class LoadEnvFileTests(TestCase):
    """.env ファイルの読み込み（config/env.py）のテスト"""

    def load(self, content):
        """一時フォルダに .env を作って読み込み、テスト後に環境変数を元に戻す"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text(content, encoding='utf-8')
            load_env_file(path)

    def test_reads_values_and_skips_comments(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.load('# コメント\n\nTEST_KEY=abc=123\nTEST_QUOTED="hello world"\nINVALID LINE\n')
            self.assertEqual(os.environ['TEST_KEY'], 'abc=123')  # 値の中の「=」は残る
            self.assertEqual(os.environ['TEST_QUOTED'], 'hello world')  # 引用符は外れる
            self.assertNotIn('INVALID LINE', os.environ)

    def test_existing_environment_variable_wins(self):
        with mock.patch.dict(os.environ, {'TEST_KEY': 'from-env'}, clear=True):
            self.load('TEST_KEY=from-file\n')
            self.assertEqual(os.environ['TEST_KEY'], 'from-env')

    def test_missing_file_does_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            load_env_file(Path(tempfile.gettempdir()) / 'no-such-dir' / '.env')
            self.assertEqual(dict(os.environ), {})


class TagBalanceChecker(HTMLParser):
    """HTMLの開始タグと終了タグが正しく対応しているかを調べる簡易チェッカー"""

    # 終了タグを書かない要素（<input> や <br> など）
    VOID_ELEMENTS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID_ELEMENTS:
            self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag in self.VOID_ELEMENTS:
            return
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append(f'{self.getpos()[0]}行目: </{tag}> に対応する開始タグがありません（開いているタグ: {self.stack[-1:]})')
            return
        self.stack.pop()

    def check(self, html):
        self.feed(html)
        self.close()
        if self.stack:
            self.errors.append(f'閉じられていないタグ: {self.stack}')
        return self.errors


class UITests(TestCase):
    """UI（画面の構成・アクセシビリティ）のテスト。日時は固定する"""

    TEMPLATES = [
        'vocab/base.html',
        'vocab/word_list.html',
        'vocab/word_confirm_delete.html',
        'vocab/review.html',
        'vocab/dashboard.html',
    ]

    def get(self, url_name, *args):
        with freeze_time(FIXED_NOW):
            return self.client.get(reverse(url_name, args=args))

    def make_reviewed_word(self, **kwargs):
        word = Word.objects.create(english='invoice', meaning='請求書', example='Please send the invoice.', **kwargs)
        ReviewLog.objects.create(word=word, is_correct=True, reviewed_at=FIXED_NOW - timedelta(hours=1))
        ReviewLog.objects.create(word=word, is_correct=False, reviewed_at=FIXED_NOW - timedelta(days=2))
        return word

    def all_pages(self):
        """主な画面を全て開き、(名前, レスポンス) のリストを返す"""
        word = Word.objects.first()
        pages = [
            ('単語一覧', self.get('vocab:word_list')),
            ('復習', self.get('vocab:review')),
            ('ダッシュボード', self.get('vocab:dashboard')),
        ]
        if word:
            pages.append(('削除確認', self.get('vocab:word_delete', word.pk)))
        return pages

    # ----- テンプレート・共通部分 -----

    def test_all_templates_compile(self):
        for name in self.TEMPLATES:
            with self.subTest(template=name):
                get_template(name)  # 文法エラーがあればここで例外になる

    def test_html_tags_are_balanced(self):
        for state in ('データなし', 'データあり'):
            if state == 'データあり':
                self.make_reviewed_word(next_review_at=FIXED_NOW)
            for name, response in self.all_pages():
                with self.subTest(state=state, page=name):
                    self.assertEqual(response.status_code, 200)
                    errors = TagBalanceChecker().check(response.content.decode())
                    self.assertEqual(errors, [])

    def test_stylesheet_is_linked_and_exists(self):
        response = self.get('vocab:word_list')
        self.assertContains(response, 'href="/static/vocab/css/style.css"')
        self.assertIsNotNone(finders.find('vocab/css/style.css'))

    def test_page_structure_and_skip_link(self):
        for name, response in self.all_pages():
            with self.subTest(page=name):
                self.assertContains(response, '<main id="main"')
                self.assertContains(response, 'href="#main"')
                self.assertContains(response, '<nav aria-label="メインメニュー">')
                self.assertContains(response, '<h1', count=1)  # 見出しの一番上は1ページに1つ

    def test_navigation_marks_current_page(self):
        word = Word.objects.create(english='invoice', meaning='請求書')
        cases = [
            (self.get('vocab:word_list'), reverse('vocab:word_list')),
            (self.get('vocab:word_delete', word.pk), reverse('vocab:word_list')),
            (self.get('vocab:review'), reverse('vocab:review')),
            (self.get('vocab:dashboard'), reverse('vocab:dashboard')),
        ]
        for response, current_url in cases:
            with self.subTest(current=current_url):
                self.assertContains(response, 'aria-current="page"', count=1)
                self.assertContains(response, f'href="{current_url}" aria-current="page"')

    # ----- 単語一覧・登録 -----

    def test_form_marks_required_and_optional_fields(self):
        response = self.get('vocab:word_list')
        self.assertContains(response, '<span class="badge badge-required">必須</span>', count=3)  # 説明文 + 英単語・意味
        self.assertContains(response, '<span class="badge badge-optional">任意</span>', count=1)  # 例文
        self.assertContains(response, '<label for="id_english">')

    def test_form_error_is_shown_next_to_field(self):
        response = self.client.post(reverse('vocab:word_list'), {'english': '', 'meaning': '請求書'})
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, '入力内容を確認してください')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'aria-describedby="id_english_error"')
        self.assertContains(response, '<ul class="errorlist" id="id_english_error">')
        self.assertContains(response, 'value="請求書"')  # 入力した内容は消えない

    def test_success_message_has_text_label(self):
        response = self.client.post(reverse('vocab:word_list'), {'english': 'invoice', 'meaning': '請求書'}, follow=True)
        self.assertContains(response, 'role="status"')
        self.assertContains(response, 'alert-success')
        self.assertContains(response, '完了')
        self.assertContains(response, '「invoice」を登録しました。')

    def test_word_list_shows_due_banner(self):
        Word.objects.create(english='invoice', meaning='請求書', next_review_at=FIXED_NOW)
        Word.objects.create(english='later', meaning='後で', next_review_at=FIXED_NOW + timedelta(days=3))
        response = self.get('vocab:word_list')
        self.assertContains(response, '今日は 1語 の復習があります。')
        self.assertContains(response, 'class="word-item is-due"', count=1)

    def test_word_list_hides_banner_when_nothing_due(self):
        Word.objects.create(english='later', meaning='後で', next_review_at=FIXED_NOW + timedelta(days=3))
        response = self.get('vocab:word_list')
        self.assertNotContains(response, 'の復習があります')

    def test_word_list_shows_details_and_safe_delete_link(self):
        word = self.make_reviewed_word(next_review_at=FIXED_NOW)
        response = self.get('vocab:word_list')
        self.assertContains(response, '正答率50%')
        self.assertContains(response, '最終復習: 2026/04/01 09:00')
        self.assertContains(response, 'lang="en">invoice</h3>')
        self.assertContains(response, f'href="{reverse("vocab:word_delete", args=[word.pk])}"')
        self.assertContains(response, 'aria-label="「invoice」を削除"')

    def test_empty_word_list_guides_registration(self):
        response = self.get('vocab:word_list')
        self.assertContains(response, 'まだ単語が登録されていません。')
        self.assertContains(response, 'href="#id_english"')

    def test_delete_page_has_cancel_and_warning(self):
        word = Word.objects.create(english='invoice', meaning='請求書')
        response = self.get('vocab:word_delete', word.pk)
        self.assertContains(response, '復習履歴も一緒に削除され')
        self.assertContains(response, f'href="{reverse("vocab:word_list")}">キャンセル</a>')

    # ----- 復習画面 -----

    def test_review_page_layout(self):
        Word.objects.create(english='invoice', meaning='請求書', next_review_at=FIXED_NOW)
        response = self.get('vocab:review')
        self.assertContains(response, '<summary class="btn btn-primary btn-lg">答えを見る</summary>', html=True)
        self.assertContains(response, '<legend>この単語を覚えていましたか？</legend>', html=True)
        self.assertContains(response, 'value="correct" class="btn btn-success btn-lg"')
        self.assertContains(response, 'value="incorrect" class="btn btn-danger btn-lg"')
        self.assertContains(response, 'id="question" lang="en">invoice</p>')

    def test_review_done_shows_completion_and_dashboard_link(self):
        Word.objects.create(english='later', meaning='後で', next_review_at=FIXED_NOW + timedelta(days=3))
        response = self.get('vocab:review')
        self.assertContains(response, '本日の復習は完了です')
        self.assertContains(response, f'<a class="btn btn-primary" href="{reverse("vocab:dashboard")}">')

    def test_review_without_words_links_to_registration(self):
        response = self.get('vocab:review')
        self.assertContains(response, '復習できる単語がありません。')
        self.assertContains(response, f'<a class="btn btn-primary" href="{reverse("vocab:word_list")}">単語を登録する</a>', html=True)

    # ----- ダッシュボード -----

    def test_dashboard_has_accessible_table(self):
        self.make_reviewed_word()
        response = self.get('vocab:dashboard')
        self.assertContains(response, '数値を表で見る')
        self.assertContains(response, '<th scope="row">', count=7)
        self.assertContains(response, '4/1(水)（今日）')
        self.assertContains(response, 'class="bar-chart" aria-hidden="true"')

    def test_dashboard_empty_state(self):
        response = self.get('vocab:dashboard')
        self.assertContains(response, 'まだ単語が登録されていません。')
        self.assertContains(response, 'まだ復習の記録がありません。')
        self.assertNotContains(response, '今日の復習を始める')

    def test_dashboard_shows_review_button_when_due(self):
        Word.objects.create(english='invoice', meaning='請求書', next_review_at=FIXED_NOW)
        response = self.get('vocab:dashboard')
        self.assertContains(response, '今日の復習を始める（残り1語）')
        self.assertNotContains(response, 'まだ単語が登録されていません。')

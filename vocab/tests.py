import os
import random
import re
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
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import dashboard as dashboard_stats
from . import scheduling
from .forms import WordForm, normalize_synonyms, parse_phrase_line, split_lines
from .models import Phrase, ReviewLog, Sense, Word

JST = ZoneInfo('Asia/Tokyo')
# テストで使う「現在日時」。本物の現在日時に左右されないよう固定する
FIXED_NOW = datetime(2026, 4, 1, 10, 0, tzinfo=JST)


def freeze_time(dt):
    """with freeze_time(...): の中では timezone.now() が dt を返すようにする"""
    return mock.patch('django.utils.timezone.now', return_value=dt)


def answer_today(client, word, result='correct', scheduled_at=None):
    """今日の復習の回答を、画面のフォームと同じ形で送る

    画面には「表示したときの次回復習予定日時」が入っているので、ここでもデータベースの今の値を付けて送る。
    scheduled_at を渡すと、その値を送る（古い画面からの送信を再現するときに使う）。
    """
    if scheduled_at is None:
        scheduled_at = Word.objects.get(pk=word.pk).next_review_at
    return client.post(reverse('vocab:review_answer', args=[word.pk]),
                       {'result': result, 'scheduled_at': scheduled_at.isoformat()})


class WordListViewTests(TestCase):
    """一覧表示と登録のテスト"""

    def test_empty_list(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'まだ単語・熟語が登録されていません。')

    def test_list_shows_words(self):
        Word.objects.create(english='negotiate', meaning='交渉する')
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, 'negotiate')
        self.assertContains(response, '交渉する')

    def test_create_word(self):
        response = self.client.post(reverse('vocab:word_list'), {
            'english': 'invoice',
            'pos_noun': 'on', 'meanings_noun': '請求書',
            'example': 'Please send the invoice.',
        })
        self.assertRedirects(response, reverse('vocab:word_list'))
        self.assertEqual(Word.objects.count(), 1)
        word = Word.objects.get()
        self.assertEqual(word.english, 'invoice')
        self.assertEqual(word.example, 'Please send the invoice.')

    def test_create_word_without_example(self):
        self.client.post(reverse('vocab:word_list'), {'english': 'deadline', 'pos_noun': 'on', 'meanings_noun': '締め切り'})
        self.assertEqual(Word.objects.count(), 1)

    def test_create_word_missing_required_field(self):
        response = self.client.post(reverse('vocab:word_list'), {'english': '', 'pos_noun': 'on', 'meanings_noun': '請求書'})
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
        self.assertContains(response, '復習できる単語・熟語がありません。')

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
        response = answer_today(self.client, self.word, 'correct')
        self.assertRedirects(response, reverse('vocab:review'))
        log = ReviewLog.objects.get()
        self.assertEqual(log.word, self.word)
        self.assertTrue(log.is_correct)
        self.assertIsNotNone(log.reviewed_at)

    def test_post_incorrect_saves_log(self):
        answer_today(self.client, self.word, 'incorrect')
        self.assertFalse(ReviewLog.objects.get().is_correct)

    def test_logs_accumulate(self):
        # 復習予定日が来るたびに回答すると、履歴は上書きされずに増えていく
        Word.objects.filter(pk=self.word.pk).update(next_review_at=FIXED_NOW)
        for result in ('correct', 'incorrect', 'correct'):
            now = Word.objects.get(pk=self.word.pk).next_review_at  # 予定日時ちょうどに回答する
            with freeze_time(now):
                answer_today(self.client, self.word, result)
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
            self.client.post(reverse('vocab:word_list'), {'english': 'invoice', 'pos_noun': 'on', 'meanings_noun': '請求書'})
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
        self.assertContains(response, '本日復習すべき単語・熟語はありません。')
        self.assertContains(response, '本日の復習対象: <strong>0件</strong>', html=True)
        self.assertContains(response, '2026/04/04')  # 次の復習予定を案内する
        self.assertNotContains(response, '答えを見る')

    def test_answer_updates_schedule_and_redirects(self):
        word = Word.objects.create(english='invoice', meaning='請求書', review_level=2, next_review_at=FIXED_NOW)
        with freeze_time(FIXED_NOW):
            response = answer_today(self.client, word, 'correct')
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
        self.assertContains(response, '0<small>件</small>', count=2)  # 登録数・今日の残り復習
        self.assertContains(response, '<span class="hbar-value">0件</span>', count=6)  # レベル0〜5

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
            self.client.post(reverse('vocab:word_list'), {'english': 'invoice', 'pos_noun': 'on', 'meanings_noun': '請求書'})
            word = Word.objects.get()
            summary = self.client.get(reverse('vocab:dashboard')).context['summary']
            self.assertEqual(summary['total_words'], 1)
            self.assertEqual(summary['due_count'], 1)

            answer_today(self.client, word, 'correct')
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
        'vocab/word_edit.html',
        'vocab/random_setup.html',
        'vocab/random_review.html',
        'vocab/random_result.html',
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
            ('ランダム復習の設定', self.get('vocab:random_setup')),
        ]
        if word:
            pages.append(('削除確認', self.get('vocab:word_delete', word.pk)))
            pages.append(('編集', self.get('vocab:word_edit', word.pk)))
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
        self.assertContains(response, 'href="/static/vocab/css/style.css?v=')
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
        # 説明文 + 英語・英単語の意味・熟語の意味（熟語の意味は「熟語・イディオム」を選んだときだけ表示される）
        self.assertContains(response, '<span class="badge badge-required">必須</span>', count=4)
        self.assertContains(response, '<span class="badge badge-optional">任意</span>', count=3)  # 熟語・同義語・例文
        self.assertContains(response, '<label for="id_english">')

    def test_form_error_is_shown_next_to_field(self):
        response = self.client.post(reverse('vocab:word_list'), {'english': '', 'pos_noun': 'on', 'meanings_noun': '請求書'})
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, '入力内容を確認してください')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'aria-describedby="id_english_error"')
        self.assertContains(response, '<ul class="errorlist" id="id_english_error">')
        self.assertContains(response, '請求書</textarea>')  # 入力した内容は消えない

    def test_success_message_has_text_label(self):
        response = self.client.post(reverse('vocab:word_list'), {'english': 'invoice', 'pos_noun': 'on', 'meanings_noun': '請求書'}, follow=True)
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
        self.assertContains(response, 'まだ単語・熟語が登録されていません。')
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
        self.assertContains(response, '復習できる単語・熟語がありません。')
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
        self.assertContains(response, 'まだ単語・熟語が登録されていません。')
        self.assertContains(response, 'まだ復習の記録がありません。')
        self.assertNotContains(response, '今日の復習を始める')

    def test_dashboard_shows_review_button_when_due(self):
        Word.objects.create(english='invoice', meaning='請求書', next_review_at=FIXED_NOW)
        response = self.get('vocab:dashboard')
        self.assertContains(response, '今日の復習を始める（残り1件）')
        self.assertNotContains(response, 'まだ単語・熟語が登録されていません。')


class WordDetailModelTests(TestCase):
    """品詞別の意味・熟語・同義語・種類（段階1で追加したモデル）のテスト"""

    def setUp(self):
        self.word = Word.objects.create(english='select', meaning='～を選ぶ')

    def test_new_fields_have_safe_defaults(self):
        self.assertEqual(self.word.entry_type, Word.ENTRY_WORD)
        self.assertEqual(self.word.synonyms, '')
        log = ReviewLog.objects.create(word=self.word, is_correct=True)
        self.assertEqual(log.mode, ReviewLog.MODE_TODAY)

    def test_existing_record_review_is_saved_as_today_mode(self):
        scheduling.record_review(self.word, True, FIXED_NOW)
        self.assertEqual(self.word.reviews.get().mode, ReviewLog.MODE_TODAY)

    def test_word_can_have_many_senses_per_part_of_speech(self):
        Sense.objects.create(word=self.word, part_of_speech='verb', meaning='～を選ぶ', order=0)
        Sense.objects.create(word=self.word, part_of_speech='verb', meaning='～を選抜する', order=1)
        Sense.objects.create(word=self.word, part_of_speech='adjective', meaning='選び抜かれた', order=2)
        self.assertEqual(
            [(s.part_of_speech, s.meaning) for s in self.word.senses.all()],
            [('verb', '～を選ぶ'), ('verb', '～を選抜する'), ('adjective', '選び抜かれた')],
        )
        self.assertEqual(str(self.word.senses.first()), '動詞: ～を選ぶ')

    def test_phrase(self):
        phrase = Phrase.objects.create(word=self.word, expression='select A from B', meaning='BからAを選ぶ')
        self.assertEqual(str(phrase), 'select A from B = BからAを選ぶ')
        self.assertEqual(list(self.word.phrases.all()), [phrase])

    def test_synonym_list(self):
        self.word.synonyms = ' choose,pick , , opt for '
        self.assertEqual(self.word.synonym_list(), ['choose', 'pick', 'opt for'])

    def test_deleting_word_deletes_senses_and_phrases(self):
        Sense.objects.create(word=self.word, part_of_speech='verb', meaning='～を選ぶ')
        Phrase.objects.create(word=self.word, expression='select A from B')
        self.word.delete()
        self.assertEqual(Sense.objects.count(), 0)
        self.assertEqual(Phrase.objects.count(), 0)

    def test_phrase_entry_can_be_reviewed_like_a_word(self):
        idiom = Word.objects.create(english='select A from B', meaning='BからAを選ぶ',
                                    entry_type=Word.ENTRY_PHRASE, next_review_at=FIXED_NOW)
        self.assertIn(idiom, scheduling.due_words(FIXED_NOW))
        scheduling.record_review(idiom, True, FIXED_NOW)
        idiom.refresh_from_db()
        self.assertEqual(idiom.review_level, 1)


class MoveMeaningToSensesMigrationTests(TestCase):
    """既存データ移行（0006: 意味 → 品詞「その他」の Sense）のテスト"""

    def run_migration_function(self):
        migration = import_module('vocab.migrations.0006_move_meaning_to_senses')
        migration.copy_meaning_to_senses(apps, None)

    def test_copies_meaning_and_keeps_schedule_and_history(self):
        apple = Word.objects.create(english='apple', meaning='りんご', review_level=2,
                                    next_review_at=FIXED_NOW + timedelta(days=3))
        ReviewLog.objects.create(word=apple, is_correct=True, reviewed_at=FIXED_NOW - timedelta(days=1))
        ReviewLog.objects.create(word=apple, is_correct=False, reviewed_at=FIXED_NOW - timedelta(days=5))

        self.run_migration_function()

        apple.refresh_from_db()
        sense = apple.senses.get()
        self.assertEqual((sense.part_of_speech, sense.meaning), ('other', 'りんご'))
        self.assertEqual(apple.meaning, 'りんご')  # 元の意味は残る
        self.assertEqual(apple.review_level, 2)  # 復習レベル・次回予定・履歴は変わらない
        self.assertEqual(apple.next_review_at, FIXED_NOW + timedelta(days=3))
        self.assertEqual(apple.reviews.count(), 2)

    def test_running_twice_does_not_duplicate(self):
        Word.objects.create(english='apple', meaning='りんご')
        self.run_migration_function()
        self.run_migration_function()
        self.assertEqual(Sense.objects.count(), 1)

    def test_skips_empty_meaning_and_words_that_already_have_senses(self):
        Word.objects.create(english='empty', meaning='')
        select = Word.objects.create(english='select', meaning='動：～を選ぶ')
        Sense.objects.create(word=select, part_of_speech='verb', meaning='～を選ぶ')
        self.run_migration_function()
        self.assertEqual(Sense.objects.count(), 1)
        self.assertEqual(select.senses.get().part_of_speech, 'verb')


# ===== 段階2：品詞別の意味・熟語・同義語の入力 =====

SELECT_POST = {
    'english': 'select',
    'pos_verb': 'on', 'meanings_verb': '～を選ぶ\n～を選抜する',
    'pos_adjective': 'on', 'meanings_adjective': '選び抜かれた',
    'phrases': 'select A from B = BからAを選ぶ\nselect out',
    'synonyms': 'choose, pick',
    'example': 'Please select one option.',
}


class InputParsingTests(TestCase):
    """入力テキストを分解する小さな関数のテスト"""

    def test_split_lines_ignores_blank_lines_and_spaces(self):
        self.assertEqual(split_lines('  ～を選ぶ \n\n\r\n～を選抜する\n   '), ['～を選ぶ', '～を選抜する'])

    def test_parse_phrase_line(self):
        cases = [
            ('select A from B = BからAを選ぶ', ('select A from B', 'BからAを選ぶ')),
            ('select A from B＝BからAを選ぶ', ('select A from B', 'BからAを選ぶ')),  # 全角の＝
            ('pick out', ('pick out', '')),  # 意味なし
            ('A = B = C', ('A', 'B = C')),  # 最初の = で分ける
            ('= 意味だけ', ('', '意味だけ')),  # 表現が空（フォームでエラーにする）
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(parse_phrase_line(line), expected)

    def test_normalize_synonyms(self):
        self.assertEqual(normalize_synonyms(' choose、pick ,，choose, '), 'choose, pick')
        self.assertEqual(normalize_synonyms(''), '')


class WordFormTests(TestCase):
    """登録フォームの入力チェックと保存のテスト"""

    def make_form(self, **overrides):
        data = {**SELECT_POST, **overrides}
        return WordForm(data={k: v for k, v in data.items() if v is not None})

    def test_saves_senses_phrases_and_synonyms(self):
        with freeze_time(FIXED_NOW):  # フォームを作った時点で新しい単語の初期値（次回予定=今）が決まる
            form = self.make_form()
            self.assertTrue(form.is_valid(), form.errors)
            word = form.save()

        self.assertEqual(
            [(s.part_of_speech, s.meaning) for s in word.senses.all()],
            [('verb', '～を選ぶ'), ('verb', '～を選抜する'), ('adjective', '選び抜かれた')],
        )
        self.assertEqual(
            [(p.expression, p.meaning) for p in word.phrases.all()],
            [('select A from B', 'BからAを選ぶ'), ('select out', '')],
        )
        self.assertEqual(word.synonyms, 'choose, pick')
        self.assertEqual(word.example, 'Please select one option.')
        self.assertEqual(word.meaning, '動：～を選ぶ、～を選抜する／形：選び抜かれた')
        # 新しい単語は従来どおり、レベル0ですぐに復習対象
        self.assertEqual(word.review_level, 0)
        self.assertEqual(word.next_review_at, FIXED_NOW)
        self.assertEqual(word.entry_type, Word.ENTRY_WORD)

    def test_other_only_summary_has_no_label(self):
        form = self.make_form(pos_verb=None, pos_adjective=None, pos_other='on', meanings_other='りんご')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().meaning, 'りんご')

    def test_unselected_part_of_speech_is_not_saved(self):
        # 形容詞のボタンを外した場合、欄に文字が残っていても保存しない
        form = self.make_form(pos_adjective=None)
        self.assertTrue(form.is_valid(), form.errors)
        word = form.save()
        self.assertEqual({s.part_of_speech for s in word.senses.all()}, {'verb'})

    def test_requires_at_least_one_meaning(self):
        cases = {
            '品詞を選んでいない': {'pos_verb': None, 'pos_adjective': None, 'meanings_verb': '', 'meanings_adjective': ''},
            '品詞を選んだが空欄': {'meanings_verb': '  \n ', 'meanings_adjective': ''},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                form = self.make_form(**overrides)
                self.assertFalse(form.is_valid())
                self.assertIn('意味を1つ以上入力してください。', form.non_field_errors()[0])

    def test_text_without_selected_button_names_the_part_of_speech(self):
        # 文字はあるがボタンが外れている場合は、どの品詞が原因かをエラーで伝える
        form = self.make_form(pos_verb=None, pos_adjective=None)
        self.assertFalse(form.is_valid())
        self.assertIn('動詞・形容詞の意味が入力されていますが、品詞のボタンが選ばれていません。', form.non_field_errors()[0])

    def test_requires_english(self):
        form = self.make_form(english='  ')
        self.assertFalse(form.is_valid())
        self.assertIn('english', form.errors)

    def test_phrase_line_with_japanese_but_no_separator_is_error(self):
        form = self.make_form(phrases='pick out\nselect A from B  BからAを選ぶ\nselect A from B ＝ BからAを選ぶ')
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['phrases'], [
            '2行目：「=」が見つかりません。表現と意味の間に「=」を入れてください（例: select A from B = BからAを選ぶ）。',
        ])  # 英語だけの行（1行目）と全角「＝」の行（3行目）は問題なし

    def test_invalid_phrase_lines_show_line_numbers(self):
        form = self.make_form(phrases='select A from B = BからAを選ぶ\n= 表現がない\n\n＝これも')
        self.assertFalse(form.is_valid())
        errors = form.errors['phrases']
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith('2行目'))
        self.assertTrue(errors[1].startswith('4行目'))

    def test_too_long_meaning_and_phrase(self):
        form = self.make_form(meanings_verb='～を選ぶ\n' + 'あ' * 201, phrases='x' * 201 + ' = 意味')
        self.assertFalse(form.is_valid())
        self.assertIn('2つ目の意味が長すぎます', form.errors['meanings_verb'][0])
        self.assertIn('1行目', form.errors['phrases'][0])

    def test_long_summary_is_truncated(self):
        form = self.make_form(meanings_verb='\n'.join(f'意味{i:03d}' for i in range(60)))
        self.assertTrue(form.is_valid(), form.errors)
        word = form.save()
        self.assertEqual(len(word.meaning), 200)
        self.assertTrue(word.meaning.endswith('…'))
        self.assertEqual(word.senses.count(), 61)  # 要約は短くしても、意味自体は全て保存する


class WordRegistrationViewTests(TestCase):
    """画面からの登録と、一覧・復習画面での表示のテスト"""

    def post_select(self, **overrides):
        data = {k: v for k, v in {**SELECT_POST, **overrides}.items() if v is not None}
        with freeze_time(FIXED_NOW):
            return self.client.post(reverse('vocab:word_list'), data)

    def test_register_and_redirect(self):
        response = self.post_select()
        self.assertRedirects(response, reverse('vocab:word_list'))
        self.assertEqual(Word.objects.get().senses.count(), 3)

    def test_invalid_input_saves_nothing_and_keeps_input(self):
        response = self.post_select(phrases='= 表現がない')
        self.assertEqual(response.status_code, 200)
        self.assertEqual((Word.objects.count(), Sense.objects.count(), Phrase.objects.count()), (0, 0, 0))
        self.assertContains(response, '1行目：「=」の前に表現を入力してください')
        self.assertContains(response, 'id="id_pos_verb" checked')  # 選んだ品詞ボタンは選ばれたまま
        self.assertContains(response, '～を選抜する</textarea>')  # 入力した意味も消えない
        self.assertContains(response, '<details class="optional-field" open>')  # エラーのある欄は開いている

    def test_missing_meaning_error_is_shown_in_meaning_area(self):
        response = self.post_select(pos_verb=None, pos_adjective=None, meanings_verb=None, meanings_adjective=None)
        self.assertEqual(Word.objects.count(), 0)
        self.assertContains(response, '意味を1つ以上入力してください。')

    def test_form_renders_pos_buttons_and_hidden_inputs(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, 'class="pos-toggle"', count=7)
        for code, label in Sense.PART_OF_SPEECH_CHOICES:
            with self.subTest(code=code):
                self.assertContains(response, f'<label for="id_pos_{code}" class="pos-button">{label}</label>', html=True)
                self.assertContains(response, f'class="pos-input pos-input-{code}"')
        self.assertContains(response, '<details class="optional-field">', count=3)  # 任意の3項目は閉じている
        self.assertContains(response, ' checked', count=1)  # 最初はどの品詞も選ばれていない（選ばれているのは種類の「英単語」だけ）
        self.assertContains(response, 'value="word" id="id_entry_type_0" checked')

    def test_word_list_shows_grouped_details(self):
        self.post_select()
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '<dt><span class="pos-badge">動詞</span></dt>', html=True)
        self.assertContains(response, '<dd>～を選ぶ、～を選抜する</dd>', html=True)
        self.assertContains(response, '<dd>選び抜かれた</dd>', html=True)
        self.assertContains(response, '<span class="phrase-expression" lang="en">select A from B</span>', html=True)
        self.assertContains(response, '<span class="phrase-meaning">BからAを選ぶ</span>', html=True)
        self.assertContains(response, '<span class="synonym" lang="en">choose</span>', html=True)
        self.assertContains(response, '<span class="synonym" lang="en">pick</span>', html=True)

    def test_review_shows_details_inside_answer(self):
        self.post_select()
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:review'))
        content = response.content.decode()
        answer = content[content.index('<details>'):content.index('</details>')]
        for text in ('～を選抜する', '選び抜かれた', 'select A from B', 'choose', 'Please select one option.'):
            with self.subTest(text=text):
                self.assertIn(text, answer)  # 答えを見るまで隠れている部分に入っている

    def test_migrated_and_legacy_words_still_display(self):
        # 段階1で移行した単語（品詞「その他」）
        apple = Word.objects.create(english='apple', meaning='りんご')
        Sense.objects.create(word=apple, part_of_speech='other', meaning='りんご')
        # 品詞別の意味がない古い形式の単語
        Word.objects.create(english='legacy', meaning='古い形式の意味')
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '<dt><span class="pos-badge">その他</span></dt>', html=True)
        self.assertContains(response, '<dd>りんご</dd>', html=True)
        self.assertContains(response, '<p class="word-meaning">古い形式の意味</p>', html=True)

    def test_word_list_query_count_does_not_grow_with_words(self):
        for i in range(3):
            self.post_select(english=f'word{i}')
        with self.assertNumQueries(4):  # 今日の件数1・単語一覧1・意味1・熟語1（単語数に関係なく一定）
            self.client.get(reverse('vocab:word_list'))


class MultiplePartsOfSpeechTests(TestCase):
    """1つの単語に複数の品詞を登録したとき、すべての意味が保存・表示されることのテスト（段階2の不具合修正）"""

    POST = {
        'english': 'select',
        'pos_verb': 'on', 'meanings_verb': '～を選ぶ',
        'pos_adjective': 'on', 'meanings_adjective': '選び抜かれた',
        'phrases': 'select A from B = BからAを選ぶ',
    }

    def post(self, **overrides):
        data = {k: v for k, v in {**self.POST, **overrides}.items() if v is not None}
        with freeze_time(FIXED_NOW):
            return self.client.post(reverse('vocab:word_list'), data, follow=True)

    def test_all_selected_parts_of_speech_are_saved(self):
        self.post(pos_noun='on', meanings_noun='選択\n精選品')
        word = Word.objects.get()
        self.assertEqual(
            [(s.part_of_speech, s.meaning) for s in word.senses.all()],
            [('noun', '選択'), ('noun', '精選品'), ('verb', '～を選ぶ'), ('adjective', '選び抜かれた')],
        )
        self.assertEqual(word.meaning, '名：選択、精選品／動：～を選ぶ／形：選び抜かれた')
        self.assertEqual([(p.expression, p.meaning) for p in word.phrases.all()], [('select A from B', 'BからAを選ぶ')])

    def test_review_answer_shows_every_part_of_speech(self):
        self.post()
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:review'))
        content = response.content.decode()
        answer = content[content.index('<details>'):content.index('</details>')]
        for text in ('動詞', '～を選ぶ', '形容詞', '選び抜かれた', 'select A from B', 'BからAを選ぶ'):
            with self.subTest(text=text):
                self.assertIn(text, answer)

    def test_word_list_shows_every_part_of_speech(self):
        response = self.post()
        self.assertContains(response, '<dt><span class="pos-badge">動詞</span></dt>', html=True)
        self.assertContains(response, '<dd>～を選ぶ</dd>', html=True)
        self.assertContains(response, '<dt><span class="pos-badge">形容詞</span></dt>', html=True)
        self.assertContains(response, '<dd>選び抜かれた</dd>', html=True)

    def test_warns_when_typed_meaning_was_not_saved(self):
        # 今回の不具合の再現：動詞の欄に入力したが、動詞のボタンが選ばれていなかった
        response = self.post(pos_verb=None)
        word = Word.objects.get()
        self.assertEqual([s.part_of_speech for s in word.senses.all()], ['adjective'])
        self.assertContains(response, '「select」を登録しました。')
        self.assertContains(response, '動詞の欄に入力がありましたが、品詞のボタンが選ばれていなかったため保存していません。')
        self.assertContains(response, 'class="alert alert-warning"')

    def test_no_warning_when_everything_is_saved(self):
        response = self.post()
        self.assertNotContains(response, 'alert-warning')

    def test_keeps_existing_schedule_of_other_words(self):
        apple = Word.objects.create(english='apple', meaning='りんご', review_level=2)
        before = Word.objects.values_list('review_level', 'next_review_at').get(pk=apple.pk)
        self.post()
        self.assertEqual(Word.objects.values_list('review_level', 'next_review_at').get(pk=apple.pk), before)


class PosButtonStyleTests(TestCase):
    """品詞ボタンのCSSが正しく効くための前提を確認する（段階2の不具合修正）"""

    def setUp(self):
        self.css = Path(finders.find('vocab/css/style.css')).read_text(encoding='utf-8')

    def test_button_rules_win_over_generic_label_rule(self):
        # 「.form-field label」（display: flex）より詳細度の高い書き方で、横並びのボタンにしている
        self.assertIn('.pos-buttons .pos-button {\n  display: inline-flex;', self.css)
        self.assertIn('.pos-buttons .pos-toggle { position: absolute; opacity: 0;', self.css)

    def test_inputs_and_hint_follow_selected_buttons(self):
        for code, _ in Sense.PART_OF_SPEECH_CHOICES:
            with self.subTest(code=code):
                self.assertIn(f'.pos-picker:has(#id_pos_{code}:checked) .pos-input-{code}', self.css)
        self.assertIn('.pos-picker:not(:has(.pos-toggle:checked)) .pos-empty-hint { display: block; }', self.css)

    def test_stylesheet_url_has_version_to_avoid_stale_cache(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, 'vocab/css/style.css?v=')


class WordEditTests(TestCase):
    """登録済みの単語の編集（段階3）のテスト"""

    def setUp(self):
        # 今の select と同じ状態：熟語の「=」が抜けて、全体が1つの表現になっている
        with freeze_time(FIXED_NOW):
            self.word = Word.objects.create(
                english='select', meaning='形：選び抜かれた', synonyms='choose',
                example='Please select one.', review_level=3,
                next_review_at=FIXED_NOW + timedelta(days=7),
            )
        Sense.objects.create(word=self.word, part_of_speech='adjective', meaning='選び抜かれた', order=0)
        Phrase.objects.create(word=self.word, expression='select A from B  BからAを選ぶ', order=0)
        self.log = ReviewLog.objects.create(word=self.word, is_correct=True)
        self.url = reverse('vocab:word_edit', args=[self.word.pk])

    def post(self, **overrides):
        data = {
            'english': 'select',
            'pos_adjective': 'on', 'meanings_adjective': '選び抜かれた',
            'phrases': 'select A from B = BからAを選ぶ',
            'synonyms': 'choose',
            'example': 'Please select one.',
        }
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        with freeze_time(FIXED_NOW + timedelta(days=1)):
            return self.client.post(self.url, data)

    def schedule(self):
        word = Word.objects.get(pk=self.word.pk)
        return (word.review_level, word.next_review_at, word.created_at)

    def test_word_list_has_edit_button(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, f'href="{self.url}"')
        self.assertContains(response, 'aria-label="「select」を編集">編集</a>')

    def test_edit_page_is_prefilled(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'value="select"')
        self.assertContains(response, 'id="id_pos_adjective" checked')  # 登録済みの品詞は選択済み
        self.assertNotContains(response, 'id="id_pos_verb" checked')
        self.assertContains(response, '選び抜かれた</textarea>')
        self.assertContains(response, 'select A from B  BからAを選ぶ</textarea>')
        self.assertContains(response, 'value="choose"')
        self.assertContains(response, 'Please select one.</textarea>')
        self.assertContains(response, '<details class="optional-field" open>', count=3)  # 入力済みの任意項目は開く
        self.assertContains(response, '復習レベル 3')

    def test_prefilled_phrase_uses_expression_equals_meaning(self):
        Phrase.objects.create(word=self.word, expression='pick out', meaning='選び出す', order=1)
        response = self.client.get(self.url)
        self.assertContains(response, 'select A from B  BからAを選ぶ\npick out = 選び出す</textarea>')

    def test_prefilled_form_saves_without_changes(self):
        # 何も変えずに保存しても内容が変わらない（初期値の形が、入力の形と一致している）
        self.word.phrases.update(expression='select A from B', meaning='BからAを選ぶ')
        Phrase.objects.create(word=self.word, expression='select out', order=1)
        form = WordForm(instance=self.word)
        data = {}
        for name in form.fields:
            value = form[name].value()
            if value is True:
                data[name] = 'on'
            elif value not in (None, False):
                data[name] = value
        response = self.client.post(self.url, data)
        self.assertRedirects(response, reverse('vocab:word_list'))
        word = Word.objects.get(pk=self.word.pk)
        self.assertEqual([(s.part_of_speech, s.meaning) for s in word.senses.all()], [('adjective', '選び抜かれた')])
        self.assertEqual([(p.expression, p.meaning) for p in word.phrases.all()],
                         [('select A from B', 'BからAを選ぶ'), ('select out', '')])

    def test_saving_broken_phrase_as_is_shows_error(self):
        # 「=」が抜けた熟語をそのまま保存しようとすると、直すように案内する（何も保存しない）
        response = self.post(phrases='select A from B  BからAを選ぶ')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1行目：「=」が見つかりません。表現と意味の間に「=」を入れてください')
        self.assertEqual([p.expression for p in self.word.phrases.all()], ['select A from B  BからAを選ぶ'])

    def test_fix_select_phrase(self):
        # 今回確認したいこと：select の熟語を「表現 = 意味」の形に直せる
        before = self.schedule()
        response = self.post(phrases='select A from B = BからAを選ぶ')
        self.assertRedirects(response, reverse('vocab:word_list'))
        phrases = list(Word.objects.get(pk=self.word.pk).phrases.all())
        self.assertEqual([(p.expression, p.meaning) for p in phrases], [('select A from B', 'BからAを選ぶ')])
        self.assertEqual(self.schedule(), before)
        page = self.client.get(reverse('vocab:word_list'))
        self.assertContains(page, '<span class="phrase-expression" lang="en">select A from B</span>', html=True)
        self.assertContains(page, '<span class="phrase-meaning">BからAを選ぶ</span>', html=True)

    def test_add_and_remove_parts_of_speech_and_edit_everything(self):
        before = self.schedule()
        response = self.post(
            english='select ', pos_verb='on', meanings_verb='～を選ぶ\n～を選抜する',
            pos_adjective=None,  # 形容詞のボタンを外す（欄に文字が残っていても保存しない）
            phrases='select A from B = BからAを選ぶ\nselect out',
            synonyms='choose、pick', example='Select one.',
        )
        self.assertRedirects(response, reverse('vocab:word_list'), fetch_redirect_response=False)
        word = Word.objects.get(pk=self.word.pk)
        self.assertEqual([(s.part_of_speech, s.meaning) for s in word.senses.all()],
                         [('verb', '～を選ぶ'), ('verb', '～を選抜する')])
        self.assertEqual(word.meaning, '動：～を選ぶ、～を選抜する')
        self.assertEqual([(p.expression, p.meaning) for p in word.phrases.all()],
                         [('select A from B', 'BからAを選ぶ'), ('select out', '')])
        self.assertEqual((word.english, word.synonyms, word.example), ('select', 'choose, pick', 'Select one.'))
        self.assertEqual(self.schedule(), before)
        # 形容詞の欄に文字が残っていたことは「注意」で知らせる
        page = self.client.get(reverse('vocab:word_list'))
        self.assertContains(page, '「select」を更新しました。')
        self.assertContains(page, '形容詞の欄に入力がありましたが')

    def test_edit_keeps_history_and_does_not_create_new_word(self):
        self.post(phrases='select A from B = BからAを選ぶ')
        self.assertEqual(Word.objects.count(), 1)
        self.assertEqual(list(ReviewLog.objects.values_list('pk', 'word_id', 'is_correct')),
                         [(self.log.pk, self.word.pk, True)])

    def test_invalid_input_changes_nothing_and_keeps_input(self):
        before = self.schedule()
        cases = {
            '意味がない': ({'pos_adjective': None, 'meanings_adjective': ''}, '意味を1つ以上入力してください。'),
            '熟語の表現がない': ({'phrases': '= BからAを選ぶ'}, '1行目：「=」の前に表現を入力してください'),
            '英単語がない': ({'english': ''}, 'errorlist'),
        }
        for label, (overrides, error) in cases.items():
            with self.subTest(label):
                response = self.post(**overrides)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, error)
                self.assertContains(response, '保存はされていません。')
                word = Word.objects.get(pk=self.word.pk)
                self.assertEqual(word.english, 'select')
                self.assertEqual([s.meaning for s in word.senses.all()], ['選び抜かれた'])
                self.assertEqual([p.expression for p in word.phrases.all()], ['select A from B  BからAを選ぶ'])
                self.assertEqual(self.schedule(), before)
        # 入力した内容はそのまま残る
        response = self.post(phrases='= BからAを選ぶ', pos_verb='on', meanings_verb='～を選ぶ')
        self.assertContains(response, 'id="id_pos_verb" checked')
        self.assertContains(response, '～を選ぶ</textarea>')
        self.assertContains(response, '= BからAを選ぶ</textarea>')

    def test_legacy_word_without_senses_is_prefilled_as_other(self):
        legacy = Word.objects.create(english='legacy', meaning='古い形式の意味')
        response = self.client.get(reverse('vocab:word_edit', args=[legacy.pk]))
        self.assertContains(response, 'id="id_pos_other" checked')
        self.assertContains(response, '古い形式の意味</textarea>')

    def test_change_migrated_other_to_correct_part_of_speech(self):
        # 段階1で「その他」に移した既存の単語を、正しい品詞に直せる
        apple = Word.objects.create(english='apple', meaning='りんご', review_level=2)
        Sense.objects.create(word=apple, part_of_speech='other', meaning='りんご')
        before = Word.objects.values_list('review_level', 'next_review_at').get(pk=apple.pk)
        with freeze_time(FIXED_NOW):
            self.client.post(reverse('vocab:word_edit', args=[apple.pk]),
                             {'english': 'apple', 'pos_noun': 'on', 'meanings_noun': 'りんご'})
        apple.refresh_from_db()
        self.assertEqual([(s.part_of_speech, s.meaning) for s in apple.senses.all()], [('noun', 'りんご')])
        self.assertEqual(apple.meaning, '名：りんご')
        self.assertEqual(Word.objects.values_list('review_level', 'next_review_at').get(pk=apple.pk), before)

    def test_other_words_are_not_changed(self):
        other = Word.objects.create(english='apple', meaning='りんご')
        Sense.objects.create(word=other, part_of_speech='other', meaning='りんご')
        self.post(phrases='select A from B = BからAを選ぶ', pos_verb='on', meanings_verb='～を選ぶ')
        self.assertEqual([(s.part_of_speech, s.meaning) for s in other.senses.all()], [('other', 'りんご')])

    def test_missing_word_returns_404(self):
        response = self.client.get(reverse('vocab:word_edit', args=[9999]))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# 段階4：重み付きランダム復習
# ---------------------------------------------------------------------------

def make_level_word(english, level, next_review_at=None):
    """指定したレベルの単語を作る（次回復習日を省略したら、まだ予定日前＝10日後にする）"""
    if next_review_at is None:
        next_review_at = FIXED_NOW + timedelta(days=10)
    word = Word.objects.create(english=english, meaning=f'{english}の意味', review_level=level,
                               next_review_at=next_review_at)
    Sense.objects.create(word=word, part_of_speech='other', meaning=f'{english}の意味')
    return word


class WeightedSampleTests(TestCase):
    """重み付き・重複なしの抽選（データベースを使わない計算部分）"""

    def test_weights_match_the_specification(self):
        self.assertEqual(scheduling.RANDOM_WEIGHTS, {0: 6, 1: 5, 2: 4, 3: 3, 4: 2, 5: 1})
        self.assertTrue(all(weight > 0 for weight in scheduling.RANDOM_WEIGHTS.values()))

    def test_no_duplicates_and_count(self):
        items = list(range(20))
        for count in (1, 10, 20):
            with self.subTest(count=count):
                picked = scheduling.weighted_sample(items, lambda i: 1, count, random.Random(count))
                self.assertEqual(len(picked), count)
                self.assertEqual(len(set(picked)), count)  # 重複なし
                self.assertTrue(set(picked) <= set(items))

    def test_count_larger_than_items_or_none_returns_everything_once(self):
        items = ['a', 'b', 'c']
        for count in (None, 3, 10):
            with self.subTest(count=count):
                picked = scheduling.weighted_sample(items, lambda i: 1, count, random.Random(1))
                self.assertEqual(sorted(picked), items)
        self.assertEqual(scheduling.weighted_sample([], lambda i: 1, 10, random.Random(1)), [])

    def test_same_seed_gives_same_result(self):
        items = list(range(30))
        first = scheduling.weighted_sample(items, lambda i: i % 6 + 1, 10, random.Random(42))
        second = scheduling.weighted_sample(items, lambda i: i % 6 + 1, 10, random.Random(42))
        self.assertEqual(first, second)

    def test_lower_level_is_picked_more_often_but_level5_is_not_zero(self):
        # Lv.0（重み6）と Lv.5（重み1）から1つ選ぶのを7000回くり返すと、およそ 6000:1000 になる
        rng = random.Random(0)
        weight = lambda level: scheduling.RANDOM_WEIGHTS[level]
        picks = [scheduling.weighted_sample([0, 5], weight, 1, rng)[0] for _ in range(7000)]
        level0 = picks.count(0)
        self.assertGreater(level0 / 7000, 0.82)
        self.assertLess(level0 / 7000, 0.89)
        self.assertGreater(picks.count(5), 0)  # 高いレベルも出題される

    def test_all_mode_weights_affect_only_the_order(self):
        # 全問の場合：毎回すべての単語が出るが、低いレベルの単語ほど先頭に来やすい
        rng = random.Random(0)
        weight = lambda level: scheduling.RANDOM_WEIGHTS[level]
        first_counts = {level: 0 for level in range(6)}
        for _ in range(3000):
            picked = scheduling.weighted_sample(list(range(6)), weight, None, rng)
            self.assertEqual(sorted(picked), list(range(6)))
            first_counts[picked[0]] += 1
        self.assertGreater(first_counts[0], first_counts[5] * 3)
        self.assertGreater(first_counts[5], 0)


class PickRandomWordsTests(TestCase):
    def setUp(self):
        self.words = {level: [make_level_word(f'lv{level}-{i}', level) for i in range(3)] for level in range(6)}

    def test_filters_by_level_without_duplicates(self):
        ids = scheduling.pick_random_words([0, 5], 10, random.Random(1))
        self.assertEqual(len(ids), 6)  # 対象は6語しかないので6問
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(set(Word.objects.filter(pk__in=ids).values_list('review_level', flat=True)), {0, 5})

    def test_count_and_all(self):
        self.assertEqual(len(scheduling.pick_random_words(range(6), 10, random.Random(1))), 10)
        all_ids = scheduling.pick_random_words(range(6), None, random.Random(1))
        self.assertEqual(sorted(all_ids), sorted(Word.objects.values_list('pk', flat=True)))

    def test_no_target_words(self):
        Word.objects.filter(review_level=3).delete()
        self.assertEqual(scheduling.pick_random_words([3], 10), [])


class RecordRandomReviewTests(TestCase):
    """ランダム復習の結果の反映（案B）"""

    def record(self, word, is_correct):
        with freeze_time(FIXED_NOW):
            updated = scheduling.record_random_review(word, is_correct)
        word.refresh_from_db()
        return updated

    def test_due_word_follows_normal_rule(self):
        for is_correct in (True, False):
            with self.subTest(is_correct=is_correct):
                word = make_level_word(f'due-{is_correct}', 2, next_review_at=FIXED_NOW - timedelta(hours=1))
                self.assertTrue(self.record(word, is_correct))
                expected = scheduling.calculate_next_review(2, is_correct, FIXED_NOW)
                self.assertEqual((word.review_level, word.next_review_at), expected)

    def test_due_later_today_counts_as_due(self):
        # 今日の23時が予定の単語も「今日の復習対象」（今日の復習と同じ締め切り）
        word = make_level_word('tonight', 1, next_review_at=FIXED_NOW + timedelta(hours=12))
        self.assertTrue(self.record(word, True))
        self.assertEqual(word.review_level, 2)

    def test_not_due_correct_records_history_only(self):
        word = make_level_word('early', 3)
        before = (word.review_level, word.next_review_at)
        self.assertFalse(self.record(word, True))
        self.assertEqual((word.review_level, word.next_review_at), before)
        log = ReviewLog.objects.get()
        self.assertEqual((log.word, log.is_correct, log.mode, log.reviewed_at), (word, True, 'random', FIXED_NOW))

    def test_not_due_incorrect_resets_to_level0_and_tomorrow(self):
        word = make_level_word('forgot', 4)
        self.assertTrue(self.record(word, False))
        self.assertEqual((word.review_level, word.next_review_at), (0, FIXED_NOW + timedelta(days=1)))
        self.assertEqual(ReviewLog.objects.get().mode, 'random')

    def test_today_review_still_records_today_mode(self):
        word = make_level_word('today', 1, next_review_at=FIXED_NOW)
        with freeze_time(FIXED_NOW):
            scheduling.record_review(word, True)
        self.assertEqual(ReviewLog.objects.get().mode, 'today')


# テストでは時刻を過去（2026/04/01）に固定するため、そのとき作ったセッションが
# 実際の現在時刻では期限切れにならないよう、このテストの間だけ有効期限を長くする
@override_settings(SESSION_COOKIE_AGE=20 * 365 * 24 * 60 * 60)
class RandomReviewViewTests(TestCase):
    """ランダム復習の画面の流れ"""

    def setUp(self):
        self.words = [make_level_word(f'word{i}', i % 6) for i in range(12)]

    def start(self, count='10', levels=None):
        if levels is None:
            levels = [str(level) for level in range(6)]
        with freeze_time(FIXED_NOW):
            return self.client.post(reverse('vocab:random_setup'), {'count': count, 'levels': levels})

    def state(self):
        return self.client.session.get('random_review')

    def question(self):
        with freeze_time(FIXED_NOW):
            return self.client.get(reverse('vocab:random_question'))

    def answer(self, result='correct', state=None, **overrides):
        state = state or self.state()
        data = {
            'result': result,
            'set_id': state['set_id'],
            'position': str(state['position']),
            'word_id': str(state['word_ids'][state['position']]) if state['position'] < len(state['word_ids']) else '0',
        }
        data.update(overrides)
        with freeze_time(FIXED_NOW):
            return self.client.post(reverse('vocab:random_answer'), data)

    # ----- 設定画面 -----

    def test_setup_page_defaults(self):
        response = self.client.get(reverse('vocab:random_setup'))
        self.assertContains(response, '<label for="id_count_0" class="choice-button">10問</label>', html=True)
        self.assertContains(response, '<label for="id_count_1" class="choice-button">30問</label>', html=True)
        self.assertContains(response, '<label for="id_count_2" class="choice-button">全問</label>', html=True)
        self.assertContains(response, 'value="10" id="id_count_0" required checked')  # 最初は10問
        for level in range(6):
            with self.subTest(level=level):
                self.assertContains(response, f'value="{level}" id="id_levels_{level}" checked')  # 最初は全レベル
                self.assertContains(response, f'Lv.{level}<span class="choice-count">2件</span>')
        self.assertContains(response, '「全問」を選ぶと、対象をすべて1回ずつ出題します')
        self.assertContains(response, '出題の順番にだけ影響します')
        self.assertContains(response, '復習を開始する')

    def test_setup_page_without_words(self):
        Word.objects.all().delete()
        response = self.client.get(reverse('vocab:random_setup'))
        self.assertContains(response, '復習できる単語・熟語がありません。')
        self.assertNotContains(response, '復習を開始する')

    def test_start_requires_level(self):
        response = self.start(levels=[])
        self.assertContains(response, '復習レベルを1つ以上選んでください。')
        self.assertIsNone(self.state())

    def test_start_with_no_target_words_shows_message(self):
        Word.objects.filter(review_level=5).delete()
        response = self.start(levels=['5'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '選んだ復習レベルの単語・熟語がありません。')
        self.assertIsNone(self.state())

    def test_start_picks_requested_number_without_duplicates(self):
        cases = [('10', None, 10), ('30', None, 12), ('all', None, 12), ('10', ['0', '1'], 4)]
        for count, levels, expected in cases:
            with self.subTest(count=count, levels=levels):
                response = self.start(count, levels)
                self.assertRedirects(response, reverse('vocab:random_question'), fetch_redirect_response=False)
                ids = self.state()['word_ids']
                self.assertEqual(len(ids), expected)
                self.assertEqual(len(set(ids)), expected)
                allowed = [int(level) for level in (levels or range(6))]
                self.assertTrue(all(Word.objects.get(pk=pk).review_level in allowed for pk in ids))
        self.assertEqual(ReviewLog.objects.count(), 0)  # 開始しただけでは何も記録しない

    # ----- 問題画面 -----

    def test_question_page(self):
        self.start()
        response = self.question()
        word = Word.objects.get(pk=self.state()['word_ids'][0])
        self.assertContains(response, '<strong>1/10問</strong>', html=True)
        self.assertContains(response, f'<p class="question" id="question" lang="en">{word.english}</p>', html=True)
        content = response.content.decode()
        answer = content[content.index('<details>'):content.index('</details>')]
        self.assertIn(f'{word.english}の意味', answer)  # 答えを見るまで隠れている
        self.assertIn('覚えていた', answer)
        self.assertContains(response, f'name="set_id" value="{self.state()["set_id"]}"')
        self.assertContains(response, 'name="position" value="0"')
        self.assertNotContains(response, '全問：対象をすべて出題')

    def test_all_mode_is_shown_on_question_page(self):
        self.start('all')
        self.assertContains(self.question(), '<strong>1/12問</strong>（全問：対象をすべて出題）', html=False)

    def test_question_without_set_redirects_to_setup(self):
        response = self.client.get(reverse('vocab:random_question'))
        self.assertRedirects(response, reverse('vocab:random_setup'))

    def test_reloading_question_does_not_record(self):
        self.start()
        self.question()
        self.question()
        self.assertEqual(ReviewLog.objects.count(), 0)
        self.assertEqual(self.state()['position'], 0)

    # ----- 回答と結果 -----

    def test_full_set_and_result(self):
        self.start('all', ['0', '1'])  # 4語
        results = ['correct', 'incorrect', 'correct', 'correct']
        for number, result in enumerate(results, start=1):
            self.assertContains(self.question(), f'<strong>{number}/4問</strong>', html=True)
            response = self.answer(result)
            self.assertRedirects(response, reverse('vocab:random_question'), fetch_redirect_response=False)
        response = self.question()
        self.assertRedirects(response, reverse('vocab:random_result'))
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:random_result'))
        self.assertContains(response, '<dt>正解</dt><dd>3<small>問</small></dd>', html=True)
        self.assertContains(response, '<dt>不正解</dt><dd>1<small>問</small></dd>', html=True)
        self.assertContains(response, '<dt>正答率</dt><dd>75<small>%</small></dd>', html=True)
        self.assertContains(response, '4問（全問）のセットが終わりました。')
        self.assertEqual(ReviewLog.objects.filter(mode='random').count(), 4)
        self.assertEqual(ReviewLog.objects.filter(mode='today').count(), 0)

    def test_answer_applies_plan_b_and_shows_message(self):
        due = make_level_word('due', 2, next_review_at=FIXED_NOW - timedelta(days=1))
        self.start(levels=['2'])  # レベル2：word2, word8, due
        state = self.state()
        state['word_ids'] = [due.pk, self.words[2].pk]  # 出題順を固定する
        session = self.client.session
        session['random_review'] = state
        session.save()

        response = self.answer('correct')
        due.refresh_from_db()
        self.assertEqual(due.review_level, 3)  # 今日の復習対象は通常どおり更新
        self.assertContains(self.client.get(response.url), '「due」: 覚えていた として記録しました。（レベル3・次回 2026/04/08）')

        before = Word.objects.values_list('review_level', 'next_review_at').get(pk=self.words[2].pk)
        response = self.answer('correct')
        self.assertEqual(Word.objects.values_list('review_level', 'next_review_at').get(pk=self.words[2].pk), before)
        page = self.client.get(response.url, follow=True)
        self.assertContains(page, '復習予定日より前なので、レベルと次回復習日は変わりません')

    def test_double_submit_records_once(self):
        self.start()
        state = self.state()
        self.answer('correct', state=state)
        response = self.answer('correct', state=state)  # 同じ画面からもう一度送信
        self.assertEqual(ReviewLog.objects.count(), 1)
        self.assertEqual(self.state()['position'], 1)
        self.assertEqual(self.state()['correct'], 1)
        self.assertContains(self.client.get(response.url), 'この問題はすでに回答済みです。')

    def test_answer_from_old_set_is_ignored(self):
        self.start()
        old_state = self.state()
        self.start()  # 新しいセットを始め直した
        response = self.answer('correct', state=old_state)
        self.assertEqual(ReviewLog.objects.count(), 0)
        self.assertContains(self.client.get(response.url), 'この回答は古い画面から送られたため、記録しませんでした。')

    def test_tampered_word_id_is_ignored(self):
        self.start()
        other = self.state()['word_ids'][1]
        self.answer('correct', word_id=str(other))
        self.assertEqual(ReviewLog.objects.count(), 0)

    def test_answer_after_finishing_is_ignored(self):
        self.start('10', ['0'])  # 2語
        state = self.state()
        self.answer()
        last_state = self.state()
        self.answer(state=last_state)
        self.answer(state=last_state)  # 最後の問題を二重送信
        self.assertEqual(ReviewLog.objects.count(), 2)
        self.assertEqual(state['set_id'], self.state()['set_id'])

    def test_answer_without_set_or_with_bad_result(self):
        with freeze_time(FIXED_NOW):
            response = self.client.post(reverse('vocab:random_answer'), {'result': 'correct', 'set_id': 'x'})
        self.assertRedirects(response, reverse('vocab:random_setup'), fetch_redirect_response=False)
        self.start()
        self.assertEqual(self.answer('maybe').status_code, 400)
        self.assertEqual(self.client.get(reverse('vocab:random_answer')).status_code, 405)
        self.assertEqual(ReviewLog.objects.count(), 0)

    def test_deleted_word_is_skipped(self):
        self.start('all', ['0'])  # 2語
        ids = self.state()['word_ids']
        Word.objects.filter(pk=ids[0]).delete()  # 出題前に削除
        response = self.question()
        self.assertContains(response, '<strong>2/2問</strong>', html=True)
        self.answer('correct')
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:random_result'))
        self.assertContains(response, '途中で削除された 1件 は出題しませんでした')
        self.assertContains(response, '<dt>正答率</dt><dd>100<small>%</small></dd>', html=True)

    def test_word_deleted_while_answering_is_not_recorded(self):
        self.start('all', ['0'])
        self.question()
        Word.objects.filter(pk=self.state()['word_ids'][0]).delete()  # 答えている間に削除
        response = self.answer('correct')
        self.assertEqual(ReviewLog.objects.count(), 0)
        self.assertEqual(self.state()['skipped'], 1)
        self.assertContains(self.client.get(response.url), '出題中の単語・熟語は削除されたため、記録しませんでした。')

    def test_all_words_deleted_shows_result_without_accuracy(self):
        self.start('all', ['0'])
        Word.objects.filter(review_level=0).delete()
        self.assertRedirects(self.question(), reverse('vocab:random_result'))
        response = self.client.get(reverse('vocab:random_result'))
        self.assertContains(response, '<span aria-label="記録なし">—</span>', html=True)

    def test_result_before_finishing_returns_to_question(self):
        self.start()
        self.assertRedirects(self.client.get(reverse('vocab:random_result')), reverse('vocab:random_question'),
                             fetch_redirect_response=False)
        Session = self.client.session
        del Session['random_review']
        Session.save()
        self.assertRedirects(self.client.get(reverse('vocab:random_result')), reverse('vocab:random_setup'))

    def test_today_review_is_unchanged(self):
        # ランダム復習を始めても、今日の復習は今までどおり動き、mode='today' で記録される
        due = make_level_word('due', 0, next_review_at=FIXED_NOW - timedelta(hours=1))
        self.start()
        with freeze_time(FIXED_NOW):
            self.assertContains(self.client.get(reverse('vocab:review')), 'due')
            answer_today(self.client, due, 'correct')
        self.assertEqual(ReviewLog.objects.get().mode, 'today')
        self.assertEqual(self.state()['position'], 0)  # ランダム復習の進み具合には影響しない

    def test_navigation_has_random_review(self):
        response = self.client.get(reverse('vocab:random_setup'))
        self.assertContains(response, f'<a href="{reverse("vocab:random_setup")}" aria-current="page">ランダム復習</a>', html=True)
        self.assertContains(response, f'<a href="{reverse("vocab:review")}">今日の復習</a>', html=True)


# ---------------------------------------------------------------------------
# 段階5：熟語・イディオムを独立した問題として登録・復習する
# ---------------------------------------------------------------------------

PHRASE_POST = {
    'entry_type': 'phrase',
    'english': 'select A from B',
    'phrase_meanings': 'BからAを選ぶ',
}


@override_settings(SESSION_COOKIE_AGE=20 * 365 * 24 * 60 * 60)  # ランダム復習のセッション用（RandomReviewViewTests と同じ理由）
class PhraseEntryTests(TestCase):

    def register(self, **overrides):
        data = {k: v for k, v in {**PHRASE_POST, **overrides}.items() if v is not None}
        with freeze_time(FIXED_NOW):
            return self.client.post(reverse('vocab:word_list'), data)

    def phrase(self):
        return Word.objects.get(entry_type=Word.ENTRY_PHRASE)

    # ----- 登録 -----

    def test_form_has_entry_type_choice(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '<label for="id_entry_type_0" class="choice-button">英単語</label>', html=True)
        self.assertContains(response, '<label for="id_entry_type_1" class="choice-button">熟語・イディオム</label>', html=True)
        self.assertContains(response, 'value="word" id="id_entry_type_0" checked')  # 最初は英単語
        self.assertContains(response, 'class="form-field entry-phrase-only"')
        self.assertContains(response, 'class="form-field pos-picker entry-word-only"')

    def test_register_phrase_as_independent_question(self):
        response = self.register()
        self.assertRedirects(response, reverse('vocab:word_list'))
        phrase = self.phrase()
        self.assertEqual(phrase.english, 'select A from B')
        self.assertEqual([(s.part_of_speech, s.meaning) for s in phrase.senses.all()], [('other', 'BからAを選ぶ')])
        self.assertEqual(phrase.meaning, 'BからAを選ぶ')
        # 英単語と同じく、レベル0で登録され、すぐに今日の復習対象になる
        self.assertEqual((phrase.review_level, phrase.next_review_at), (0, FIXED_NOW))

    def test_multiple_meanings_and_extra_fields(self):
        self.register(phrase_meanings='BからAを選ぶ\nBの中からAを選び出す', synonyms='choose A from B',
                      example='Select one item from the list.')
        phrase = self.phrase()
        self.assertEqual([s.meaning for s in phrase.senses.all()], ['BからAを選ぶ', 'Bの中からAを選び出す'])
        self.assertEqual(phrase.meaning, 'BからAを選ぶ、Bの中からAを選び出す')
        self.assertEqual((phrase.synonyms, phrase.example), ('choose A from B', 'Select one item from the list.'))

    def test_phrase_requires_meaning(self):
        for meanings in (None, '', '  \n '):
            with self.subTest(meanings=meanings):
                response = self.register(phrase_meanings=meanings)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '熟語・イディオムの意味を1つ以上入力してください。')
                self.assertEqual(Word.objects.count(), 0)
        response = self.register(phrase_meanings='あ' * 201)
        self.assertContains(response, '1つ目の意味が長すぎます（200文字以内）。')
        self.assertContains(response, 'value="phrase" id="id_entry_type_1" checked')  # 選んだ種類は残る

    def test_phrase_ignores_part_of_speech_fields(self):
        # 熟語を選んだときは品詞の欄は表示されないので、文字が残っていても使わない（注意も出さない）
        response = self.register(pos_verb='on', meanings_verb='使われない意味')
        phrase = self.phrase()
        self.assertEqual([(s.part_of_speech, s.meaning) for s in phrase.senses.all()], [('other', 'BからAを選ぶ')])
        self.assertNotContains(self.client.get(response.url), 'alert-warning')

    def test_word_ignores_phrase_meaning_field(self):
        self.register(entry_type='word', english='select', pos_verb='on', meanings_verb='～を選ぶ')
        word = Word.objects.get()
        self.assertEqual(word.entry_type, Word.ENTRY_WORD)
        self.assertEqual([(s.part_of_speech, s.meaning) for s in word.senses.all()], [('verb', '～を選ぶ')])

    def test_missing_entry_type_means_word(self):
        self.register(entry_type=None, english='select', pos_verb='on', meanings_verb='～を選ぶ')
        self.assertEqual(Word.objects.get().entry_type, Word.ENTRY_WORD)

    def test_invalid_entry_type_is_error(self):
        response = self.register(entry_type='sentence')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Word.objects.count(), 0)

    def test_related_expressions_are_not_converted(self):
        # 英単語の「熟語・関連表現」欄は今までどおり。独立した問題は勝手に作らない
        self.register(entry_type='word', english='select', pos_verb='on', meanings_verb='～を選ぶ',
                      phrases='select A from B = BからAを選ぶ')
        self.assertEqual(Word.objects.count(), 1)
        self.assertEqual(Phrase.objects.count(), 1)
        # 同じ表現を独立した熟語として登録しても、単語側の関連表現はそのまま
        self.register()
        self.assertEqual(Word.objects.count(), 2)
        self.assertEqual(list(Phrase.objects.values_list('word__english', 'expression', 'meaning')),
                         [('select', 'select A from B', 'BからAを選ぶ')])

    # ----- 表示 -----

    def test_word_list_shows_phrase_badge_and_meaning(self):
        self.register()
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '<span class="badge badge-phrase">熟語</span>', count=1, html=True)
        self.assertContains(response, '<p class="word-meaning">BからAを選ぶ</p>', html=True)
        self.assertNotContains(response, 'pos-badge')  # 熟語には品詞のラベルを付けない

    # ----- 復習 -----

    def test_today_review_includes_phrase_with_same_algorithm(self):
        self.register()
        phrase = self.phrase()
        with freeze_time(FIXED_NOW):
            response = self.client.get(reverse('vocab:review'))
            self.assertContains(response, '<p class="question" id="question" lang="en">select A from B</p>', html=True)
            self.assertContains(response, '<span class="badge badge-phrase">熟語</span>', html=True)
            self.assertContains(response, '<legend>この熟語を覚えていましたか？</legend>', html=True)
            content = response.content.decode()
            self.assertIn('BからAを選ぶ', content[content.index('<details>'):content.index('</details>')])
            answer_today(self.client, phrase, 'correct')
        phrase.refresh_from_db()
        self.assertEqual((phrase.review_level, phrase.next_review_at),
                         scheduling.calculate_next_review(0, True, FIXED_NOW))
        self.assertEqual(ReviewLog.objects.get().mode, 'today')

    def test_random_review_includes_phrase(self):
        self.register()
        make_level_word('apple', 0)
        phrase = self.phrase()
        with freeze_time(FIXED_NOW):
            self.client.post(reverse('vocab:random_setup'), {'count': 'all', 'levels': ['0']})
        state = self.client.session['random_review']
        self.assertIn(phrase.pk, state['word_ids'])
        # 熟語の問題まで進めて「覚えていなかった」と答える
        position = state['word_ids'].index(phrase.pk)
        for index in range(position + 1):
            state = self.client.session['random_review']
            result = 'incorrect' if index == position else 'correct'
            with freeze_time(FIXED_NOW):
                if index == position:
                    self.assertContains(self.client.get(reverse('vocab:random_question')),
                                        '<span class="badge badge-phrase">熟語</span>', html=True)
                self.client.post(reverse('vocab:random_answer'), {
                    'result': result, 'set_id': state['set_id'], 'position': str(index),
                    'word_id': str(state['word_ids'][index])})
        phrase.refresh_from_db()
        self.assertEqual((phrase.review_level, phrase.next_review_at), (0, FIXED_NOW + timedelta(days=1)))
        self.assertEqual(ReviewLog.objects.get(word=phrase).mode, 'random')

    # ----- 編集・削除 -----

    def test_edit_phrase_is_prefilled_and_keeps_schedule(self):
        self.register()
        phrase = self.phrase()
        Word.objects.filter(pk=phrase.pk).update(review_level=3, next_review_at=FIXED_NOW + timedelta(days=7))
        url = reverse('vocab:word_edit', args=[phrase.pk])
        response = self.client.get(url)
        self.assertContains(response, '<h1 id="edit-title">熟語を編集</h1>', html=True)
        self.assertContains(response, 'value="phrase" id="id_entry_type_1" checked')
        self.assertContains(response, 'BからAを選ぶ</textarea>')
        self.assertNotContains(response, 'id="id_pos_other" checked')  # 品詞のボタンは使わない

        with freeze_time(FIXED_NOW + timedelta(days=1)):
            response = self.client.post(url, {**PHRASE_POST, 'phrase_meanings': 'Bの中からAを選ぶ'})
        self.assertRedirects(response, reverse('vocab:word_list'), fetch_redirect_response=False)
        phrase.refresh_from_db()
        self.assertEqual([s.meaning for s in phrase.senses.all()], ['Bの中からAを選ぶ'])
        self.assertEqual((phrase.review_level, phrase.next_review_at), (3, FIXED_NOW + timedelta(days=7)))

    def test_change_entry_type_by_editing(self):
        # 英単語として登録したものを、編集で熟語に変えられる（その逆も）
        self.register(entry_type='word', english='look forward to', pos_other='on', meanings_other='～を楽しみにする')
        word = Word.objects.get()
        url = reverse('vocab:word_edit', args=[word.pk])
        self.client.post(url, {'entry_type': 'phrase', 'english': 'look forward to', 'phrase_meanings': '～を楽しみにする'})
        word.refresh_from_db()
        self.assertEqual(word.entry_type, Word.ENTRY_PHRASE)
        self.client.post(url, {'entry_type': 'word', 'english': 'look forward to', 'pos_verb': 'on', 'meanings_verb': '～を楽しみにする'})
        word.refresh_from_db()
        self.assertEqual((word.entry_type, word.meaning), (Word.ENTRY_WORD, '動：～を楽しみにする'))

    def test_delete_phrase(self):
        self.register()
        phrase = self.phrase()
        with freeze_time(FIXED_NOW):
            answer_today(self.client, phrase, 'correct')
        response = self.client.post(reverse('vocab:word_delete', args=[phrase.pk]))
        self.assertRedirects(response, reverse('vocab:word_list'))
        self.assertEqual((Word.objects.count(), Sense.objects.count(), ReviewLog.objects.count()), (0, 0, 0))

    def test_css_switches_meaning_inputs_by_entry_type(self):
        css = Path(finders.find('vocab/css/style.css')).read_text(encoding='utf-8')
        self.assertIn('form:has(input[name="entry_type"][value="phrase"]:checked) .entry-word-only { display: none; }', css)
        self.assertIn('form:has(input[name="entry_type"][value="phrase"]:checked) .entry-phrase-only { display: block; }', css)


# ---------------------------------------------------------------------------
# 今日の復習の二重記録防止（最終チェックでの修正）
# ---------------------------------------------------------------------------

class TodayReviewDoubleSubmitTests(TestCase):

    def setUp(self):
        self.word = make_level_word('apple', 1, next_review_at=FIXED_NOW - timedelta(hours=1))

    def shown_at(self, now=FIXED_NOW):
        """今日の復習画面を開き、フォームに入っている「表示したときの次回復習予定日時」を取り出す"""
        with freeze_time(now):
            response = self.client.get(reverse('vocab:review'))
        match = re.search(r'name="scheduled_at" value="([^"]+)"', response.content.decode())
        self.assertIsNotNone(match, '復習画面のフォームに予定日時が入っていない')
        return datetime.fromisoformat(match.group(1))

    def answer(self, scheduled_at, result='correct', now=FIXED_NOW):
        with freeze_time(now):
            response = answer_today(self.client, self.word, result, scheduled_at=scheduled_at)
        self.word.refresh_from_db()
        return response

    def messages_after(self, response):
        return [str(message) for message in self.client.get(response.url).context['messages']]

    def test_page_contains_current_schedule(self):
        self.assertEqual(self.shown_at(), self.word.next_review_at)

    def test_double_click_records_once(self):
        shown_at = self.shown_at()
        self.answer(shown_at)
        response = self.answer(shown_at)  # 同じ画面から2回目の送信
        self.assertEqual(ReviewLog.objects.count(), 1)
        self.assertEqual((self.word.review_level, self.word.next_review_at),
                         scheduling.calculate_next_review(1, True, FIXED_NOW))  # レベルは1つだけ上がる
        self.assertIn('「apple」への回答は、すでに記録済みか古い画面から送られたため、記録しませんでした。',
                      self.messages_after(response))

    def test_back_button_resubmit_with_other_answer_is_ignored(self):
        # 「覚えていなかった」と答えた後、「戻る」で前の画面に戻って「覚えていた」を押し直した
        shown_at = self.shown_at()
        self.answer(shown_at, 'incorrect')
        self.answer(shown_at, 'correct', now=FIXED_NOW + timedelta(minutes=1))
        self.assertEqual(list(ReviewLog.objects.values_list('is_correct', flat=True)), [False])
        self.assertEqual((self.word.review_level, self.word.next_review_at), (0, FIXED_NOW + timedelta(days=1)))

    def test_can_answer_again_when_due_next_day(self):
        self.answer(self.shown_at(), 'incorrect')  # 次回は翌日の同じ時刻
        next_day = FIXED_NOW + timedelta(days=1)
        shown_at = self.shown_at(next_day)  # 翌日、改めて今日の復習画面を開く
        self.answer(shown_at, 'correct', now=next_day)
        self.assertEqual(ReviewLog.objects.count(), 2)
        self.assertEqual((self.word.review_level, self.word.next_review_at),
                         scheduling.calculate_next_review(0, True, next_day))

    def test_stale_page_from_previous_day_is_ignored(self):
        # 前日に開いたままの画面から、翌日（もう一度復習対象になった日）に送信しても記録しない
        old_shown_at = self.shown_at()
        self.answer(old_shown_at, 'incorrect')
        next_day = FIXED_NOW + timedelta(days=1, hours=1)
        self.answer(old_shown_at, 'correct', now=next_day)
        self.assertEqual(ReviewLog.objects.count(), 1)
        self.assertEqual(self.word.review_level, 0)
        # 画面を開き直せば、正しく回答できる
        self.answer(self.shown_at(next_day), 'correct', now=next_day)
        self.assertEqual(ReviewLog.objects.count(), 2)
        self.assertEqual(self.word.review_level, 1)

    def test_missing_or_broken_schedule_is_ignored(self):
        url = reverse('vocab:review_answer', args=[self.word.pk])
        for data in ({'result': 'correct'}, {'result': 'correct', 'scheduled_at': 'abc'}):
            with self.subTest(data=data), freeze_time(FIXED_NOW):
                response = self.client.post(url, data)
                self.assertRedirects(response, reverse('vocab:review'), fetch_redirect_response=False)
        self.assertEqual(ReviewLog.objects.count(), 0)

    def test_word_not_due_today_is_ignored(self):
        # 予定日時は合っていても、今日の復習対象でない単語への回答は記録しない
        Word.objects.filter(pk=self.word.pk).update(next_review_at=FIXED_NOW + timedelta(days=3))
        response = self.answer(FIXED_NOW + timedelta(days=3))
        self.assertEqual(ReviewLog.objects.count(), 0)
        self.assertEqual(self.word.review_level, 1)
        self.assertIn('「apple」は今日の復習対象ではないため、記録しませんでした。', self.messages_after(response))

    def test_timezone_of_schedule_does_not_matter(self):
        # 同じ日時なら、日本時間で書いても UTC で書いても同じものとして扱う
        self.answer(self.shown_at().astimezone(JST))
        self.assertEqual(ReviewLog.objects.count(), 1)

    @override_settings(SESSION_COOKIE_AGE=20 * 365 * 24 * 60 * 60)
    def test_today_page_becomes_stale_after_random_review(self):
        # 今日の復習画面を開いたまま、ランダム復習で同じ単語に回答した → 今日の復習画面からの回答は記録しない
        shown_at = self.shown_at()
        with freeze_time(FIXED_NOW):
            self.client.post(reverse('vocab:random_setup'), {'count': '10', 'levels': ['1']})
            state = self.client.session['random_review']
            self.client.post(reverse('vocab:random_answer'), {
                'result': 'correct', 'set_id': state['set_id'], 'position': '0', 'word_id': str(self.word.pk)})
        self.answer(shown_at, 'correct')
        self.assertEqual(list(ReviewLog.objects.values_list('mode', flat=True)), ['random'])
        self.assertEqual(self.word.review_level, 2)  # ランダム復習の分（今日の対象なので通常どおり）だけ上がる

    def test_answers_are_processed_one_at_a_time(self):
        # 確認〜記録の間は鍵（Lock）をかけ、ほぼ同時に届いた送信も1件ずつ順番に処理する
        lock = mock.MagicMock()
        with mock.patch('vocab.views.ANSWER_LOCK', lock):
            self.answer(self.shown_at())
        lock.__enter__.assert_called_once()
        lock.__exit__.assert_called_once()
        self.assertEqual(ReviewLog.objects.count(), 1)

    def test_lock_is_released_after_rejected_answer(self):
        from vocab.views import ANSWER_LOCK
        self.answer(FIXED_NOW + timedelta(days=9))  # 予定日時が合わない回答
        self.assertFalse(ANSWER_LOCK.locked())
        self.answer(self.shown_at())
        self.assertEqual(ReviewLog.objects.count(), 1)


class WordingTests(TestCase):
    """単語と熟語の両方を扱うようになったことに合わせた文言（最終チェックでの修正）"""

    def test_dashboard_counts_words_and_phrases(self):
        make_level_word('apple', 0)
        phrase = make_level_word('select A from B', 0)
        Word.objects.filter(pk=phrase.pk).update(entry_type=Word.ENTRY_PHRASE)
        response = self.client.get(reverse('vocab:dashboard'))
        self.assertContains(response, '<span class="summary-label">登録数（単語・熟語）</span>', html=True)
        self.assertContains(response, '<span class="summary-value">2<small>件</small></span>', html=True)
        self.assertContains(response, '復習レベル別の登録数')
        self.assertNotContains(response, '登録単語数')

    def test_delete_confirmation_names_word_or_phrase(self):
        word = make_level_word('apple', 0)
        phrase = make_level_word('select A from B', 0)
        Word.objects.filter(pk=phrase.pk).update(entry_type=Word.ENTRY_PHRASE)
        response = self.client.get(reverse('vocab:word_delete', args=[word.pk]))
        self.assertContains(response, 'この単語の復習履歴も一緒に削除され')
        response = self.client.get(reverse('vocab:word_delete', args=[phrase.pk]))
        self.assertContains(response, 'この熟語の復習履歴も一緒に削除され')

    def test_word_list_wording(self):
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '「今日の復習」から復習できます')
        self.assertNotContains(response, '「復習する」')
        self.assertContains(response, '>入力欄へ移動</a>')
        make_level_word('apple', 0)
        response = self.client.get(reverse('vocab:word_list'))
        self.assertContains(response, '<h2 id="list-title">登録した単語・熟語</h2>', html=True)
        self.assertContains(response, '<p>1件</p>', html=True)

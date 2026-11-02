from django.db import models
from django.db.models import Case, Count, F, FloatField, Max, Q, When
from django.utils import timezone


class WordQuerySet(models.QuerySet):
    """Word を取り出すときに使える便利メソッドをまとめたもの"""

    def with_stats(self):
        """各単語に復習の成績（集計結果）を付けて返す

        付く項目:
            review_count    復習回数
            correct_count   正解回数
            incorrect_count 不正解回数
            accuracy        正答率（0〜100）。未復習なら None
            last_reviewed_at 最終復習日時。未復習なら None

        集計はデータベース側で行うので、将来の復習アルゴリズムでも
        Word.objects.with_stats().order_by('accuracy') のように並べ替えに使える。
        """
        return self.annotate(
            review_count=Count('reviews'),
            correct_count=Count('reviews', filter=Q(reviews__is_correct=True)),
            incorrect_count=Count('reviews', filter=Q(reviews__is_correct=False)),
            last_reviewed_at=Max('reviews__reviewed_at'),
        ).annotate(
            accuracy=Case(
                When(review_count=0, then=None),  # 0で割らないように未復習は None
                default=100.0 * F('correct_count') / F('review_count'),
                output_field=FloatField(),
            ),
        )


def default_next_review_at():
    """登録直後の単語の次回復習予定日時 = 今（すぐに復習対象になる）

    default=timezone.now と直接書かずに関数を挟むのは、
    テストで timezone.now を固定したときにもその時刻が使われるようにするため。
    """
    return timezone.now()


class Word(models.Model):
    """TOEIC単語を1件ずつ保存するモデル（データベースの表）"""

    english = models.CharField('英単語', max_length=100)
    meaning = models.CharField('意味', max_length=200)
    example = models.TextField('例文', blank=True)  # blank=True: 空欄でもOK
    created_at = models.DateTimeField('登録日時', auto_now_add=True)  # 登録時に自動で記録
    # 復習スケジュール用の項目（計算方法は vocab/scheduling.py を参照）
    review_level = models.PositiveSmallIntegerField('復習レベル', default=0)
    next_review_at = models.DateTimeField('次回復習予定日時', default=default_next_review_at, db_index=True)

    objects = WordQuerySet.as_manager()  # Word.objects.with_stats() を使えるようにする

    class Meta:
        ordering = ['-created_at']  # 新しく登録した順に並べる
        verbose_name = '単語'
        verbose_name_plural = '単語'

    def __str__(self):
        return self.english


class ReviewLog(models.Model):
    """1回の復習結果を1件ずつ保存するモデル

    復習するたびに新しい行を追加するだけなので、過去の記録は消えずに蓄積される。
    """

    # on_delete=CASCADE: 単語を削除したら、その単語の復習履歴も一緒に削除する
    # related_name='reviews': word.reviews.all() でその単語の履歴を取り出せる
    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name='reviews', verbose_name='単語')
    is_correct = models.BooleanField('覚えていた')
    reviewed_at = models.DateTimeField('復習日時', default=timezone.now)

    class Meta:
        ordering = ['-reviewed_at']  # 新しい履歴から順に並べる
        indexes = [
            # 「この単語の最近の履歴」を素早く探せるようにする（将来のアルゴリズム用）
            models.Index(fields=['word', 'reviewed_at']),
        ]
        verbose_name = '復習履歴'
        verbose_name_plural = '復習履歴'

    def __str__(self):
        result = '○' if self.is_correct else '×'
        return f'{self.word.english} {result} ({self.reviewed_at:%Y-%m-%d %H:%M})'

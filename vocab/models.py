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
    """TOEIC単語（または熟語）を1件ずつ保存するモデル（データベースの表）

    1件が復習の1問になる。品詞別の意味は Sense、関連する熟語・表現は Phrase に保存する。
    """

    # 見出しの種類：単語として登録するか、熟語・表現そのものを1問として登録するか
    ENTRY_WORD = 'word'
    ENTRY_PHRASE = 'phrase'
    ENTRY_TYPE_CHOICES = [
        (ENTRY_WORD, '単語'),
        (ENTRY_PHRASE, '熟語・表現'),
    ]

    english = models.CharField('英単語', max_length=100)
    # 意味の要約（一覧や管理画面で使う）。品詞別の意味は Sense が正式な保存先
    meaning = models.CharField('意味（要約）', max_length=200, blank=True)
    example = models.TextField('例文', blank=True)  # blank=True: 空欄でもOK
    synonyms = models.CharField('同義語', max_length=200, blank=True, help_text='カンマ区切り（例: choose, pick）')
    entry_type = models.CharField('種類', max_length=10, choices=ENTRY_TYPE_CHOICES, default=ENTRY_WORD)
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

    def synonym_list(self):
        """同義語を ['choose', 'pick'] のようなリストで返す（空の項目は除く）"""
        return [s.strip() for s in self.synonyms.split(',') if s.strip()]

    def grouped_senses(self):
        """品詞別の意味を、品詞の順番でまとめて返す（画面表示用）

        例: [{'code': 'verb', 'label': '動詞', 'meanings': ['～を選ぶ']},
             {'code': 'adjective', 'label': '形容詞', 'meanings': ['選び抜かれた']}]
        """
        meanings_by_pos = {}
        for sense in self.senses.all():  # 一覧画面では prefetch_related で先に読み込んでおく
            meanings_by_pos.setdefault(sense.part_of_speech, []).append(sense.meaning)
        return [
            {'code': code, 'label': label, 'meanings': meanings_by_pos[code]}
            for code, label in Sense.PART_OF_SPEECH_CHOICES
            if code in meanings_by_pos
        ]


def summarize_senses(senses):
    """(品詞, 意味) の組のリストから、一覧などで使う短い要約を作る

    例: [('verb', '～を選ぶ'), ('adjective', '選び抜かれた')] → '動：～を選ぶ／形：選び抜かれた'
    品詞が「その他」だけのときは、ラベルを付けずに意味だけを並べる。
    """
    meanings_by_pos = {}
    for pos, meaning in senses:
        meanings_by_pos.setdefault(pos, []).append(meaning)

    if list(meanings_by_pos) == ['other']:
        summary = '、'.join(meanings_by_pos['other'])
    else:
        summary = '／'.join(
            f'{Sense.SHORT_LABELS[code]}：' + '、'.join(meanings_by_pos[code])
            for code, _ in Sense.PART_OF_SPEECH_CHOICES
            if code in meanings_by_pos
        )

    max_length = Word._meta.get_field('meaning').max_length
    if len(summary) > max_length:
        summary = summary[:max_length - 1] + '…'
    return summary


class Sense(models.Model):
    """品詞ごとの意味を1件ずつ保存するモデル

    1つの単語に複数の品詞があっても、1つの品詞に複数の意味があっても、行を増やすだけで保存できる。
    例: select → (動詞, ～を選ぶ), (形容詞, 選び抜かれた)
    """

    # 品詞の一覧（画面のボタンもこの順番で並べる）
    PART_OF_SPEECH_CHOICES = [
        ('noun', '名詞'),
        ('verb', '動詞'),
        ('adjective', '形容詞'),
        ('adverb', '副詞'),
        ('preposition', '前置詞'),
        ('conjunction', '接続詞'),
        ('other', 'その他'),
    ]
    # 要約で使う1文字の略称
    SHORT_LABELS = {
        'noun': '名', 'verb': '動', 'adjective': '形', 'adverb': '副',
        'preposition': '前', 'conjunction': '接', 'other': '他',
    }

    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name='senses', verbose_name='単語')
    part_of_speech = models.CharField('品詞', max_length=20, choices=PART_OF_SPEECH_CHOICES)
    meaning = models.CharField('意味', max_length=200)
    order = models.PositiveSmallIntegerField('表示順', default=0)

    class Meta:
        ordering = ['order', 'pk']
        verbose_name = '品詞別の意味'
        verbose_name_plural = '品詞別の意味'

    def __str__(self):
        return f'{self.get_part_of_speech_display()}: {self.meaning}'


class Phrase(models.Model):
    """単語に関連する熟語・イディオム・表現を保存するモデル（単語カードの中に表示する）

    熟語そのものを1問として復習したい場合は、Word を entry_type='phrase' で登録する。
    """

    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name='phrases', verbose_name='単語')
    expression = models.CharField('表現', max_length=200)
    meaning = models.CharField('意味', max_length=200, blank=True)
    order = models.PositiveSmallIntegerField('表示順', default=0)

    class Meta:
        ordering = ['order', 'pk']
        verbose_name = '熟語・関連表現'
        verbose_name_plural = '熟語・関連表現'

    def __str__(self):
        return f'{self.expression} = {self.meaning}' if self.meaning else self.expression


class ReviewLog(models.Model):
    """1回の復習結果を1件ずつ保存するモデル

    復習するたびに新しい行を追加するだけなので、過去の記録は消えずに蓄積される。
    """

    # on_delete=CASCADE: 単語を削除したら、その単語の復習履歴も一緒に削除する
    # related_name='reviews': word.reviews.all() でその単語の履歴を取り出せる
    # どの画面で復習したか（既存の記録はすべて「今日の復習」）
    MODE_TODAY = 'today'
    MODE_RANDOM = 'random'
    MODE_CHOICES = [
        (MODE_TODAY, '今日の復習'),
        (MODE_RANDOM, 'ランダム復習'),
    ]

    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name='reviews', verbose_name='単語')
    is_correct = models.BooleanField('覚えていた')
    reviewed_at = models.DateTimeField('復習日時', default=timezone.now)
    mode = models.CharField('復習の種類', max_length=10, choices=MODE_CHOICES, default=MODE_TODAY)

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

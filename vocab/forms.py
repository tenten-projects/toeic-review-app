import re

from django import forms
from django.db import transaction

from .models import Phrase, Sense, Word, summarize_senses

# 熟語の「表現」と「意味」を区切る記号（半角・全角のどちらでもよい）
PHRASE_SEPARATORS = ('=', '＝')
# 同義語を区切る記号（半角カンマ・全角カンマ・読点）
SYNONYM_SEPARATORS = r'[,，、]'
# ひらがな・カタカナ・漢字（熟語の行に「=」がないのに日本語があれば、「=」の入れ忘れと考える）
JAPANESE_CHARACTERS = re.compile(r'[\u3040-\u30ff\u4e00-\u9fff]')


def split_lines(text):
    """テキストを1行ずつに分け、前後の空白を取り、空の行を除く"""
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_phrase_line(line):
    """熟語の1行を (表現, 意味) に分ける

    'select A from B = BからAを選ぶ' → ('select A from B', 'BからAを選ぶ')
    'pick out'（区切りなし）          → ('pick out', '')
    最初の「=」で分けるので、意味の中に「=」があってもそのまま残る。
    """
    positions = [line.find(sep) for sep in PHRASE_SEPARATORS if sep in line]
    if not positions:
        return line.strip(), ''
    index = min(positions)
    return line[:index].strip(), line[index + 1:].strip()


def normalize_synonyms(text):
    """'choose、pick ,choose' → 'choose, pick'（区切りをそろえ、空と重複を除く）"""
    synonyms = []
    for item in re.split(SYNONYM_SEPARATORS, text):
        item = item.strip()
        if item and item not in synonyms:
            synonyms.append(item)
    return ', '.join(synonyms)


class WordForm(forms.ModelForm):
    """単語の登録・編集フォーム（同じフォームを両方の画面で使う）

    - 種類は「英単語」か「熟語・イディオム」。熟語も英単語と同じように1問として復習する
    - 英単語の意味は品詞ごとの入力欄に「1行に1つ」入力する（品詞ボタンを押した欄だけを保存する）
    - 熟語・イディオムの意味は、品詞を選ばずに「1行に1つ」入力する
    - 熟語・関連表現は「表現 = 意味」の形で1行に1つ
    - 同義語はカンマ区切り
    """

    phrases = forms.CharField(
        label='熟語・関連表現',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': '例: select A from B = BからAを選ぶ'}),
    )

    phrase_meanings = forms.CharField(
        label='意味（1行に1つ）',
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': '例: BからAを選ぶ'}),
    )

    # 画面に出す種類の名前（データベースに保存する値は 'word' / 'phrase' のまま）
    ENTRY_TYPE_LABELS = [(Word.ENTRY_WORD, '英単語'), (Word.ENTRY_PHRASE, '熟語・イディオム')]

    class Meta:
        model = Word
        fields = ['entry_type', 'english', 'synonyms', 'example']
        labels = {'english': '英語', 'synonyms': '同義語'}
        widgets = {
            'entry_type': forms.RadioSelect,
            'english': forms.TextInput(attrs={'placeholder': '例: select ／ 熟語なら select A from B'}),
            'synonyms': forms.TextInput(attrs={'placeholder': '例: choose, pick'}),
            'example': forms.Textarea(attrs={'rows': 2, 'placeholder': '例: Please select one option from the list.'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 種類は送られてこなければ「英単語」として扱う（種類の選択がなかった頃と同じ動き）
        self.fields['entry_type'].choices = self.ENTRY_TYPE_LABELS
        self.fields['entry_type'].required = False
        # 品詞ごとに「ボタン（チェックボックス）」と「意味の入力欄」を1組ずつ作る
        for code, label in Sense.PART_OF_SPEECH_CHOICES:
            self.fields[f'pos_{code}'] = forms.BooleanField(
                label=label,
                required=False,
                widget=forms.CheckboxInput(attrs={'class': 'pos-toggle'}),
            )
            self.fields[f'meanings_{code}'] = forms.CharField(
                label=f'{label}の意味（1行に1つ）',
                required=False,
                widget=forms.Textarea(attrs={'rows': 2, 'placeholder': '1行に1つの意味を入力'}),
            )
        self.cleaned_senses = []
        self.cleaned_phrases = []
        # ボタンが選ばれていないのに意味の欄に文字があった品詞（保存しないが、利用者に知らせる）
        self.ignored_pos_labels = []

        # 編集画面：登録済みの意味・熟語を入力欄に入れ、登録済みの品詞のボタンを選んだ状態にする
        # （送信後の再表示では、送られた内容をそのまま表示するので何もしない）
        if self.instance.pk and not self.is_bound:
            self.initial.update(self.initial_from_instance(self.instance))

    @staticmethod
    def initial_from_instance(word):
        """登録済みの単語から、フォームの初期値（品詞ボタン・意味・熟語）を作る"""
        initial = {}
        initial['phrases'] = '\n'.join(
            f'{phrase.expression} = {phrase.meaning}' if phrase.meaning else phrase.expression
            for phrase in word.phrases.all()
        )
        if word.entry_type == Word.ENTRY_PHRASE:
            # 熟語・イディオムは品詞を使わないので、意味をまとめて1つの欄に入れる
            meanings = [sense.meaning for sense in word.senses.all()] or ([word.meaning] if word.meaning else [])
            initial['phrase_meanings'] = '\n'.join(meanings)
            return initial

        groups = word.grouped_senses()
        if not groups and word.meaning:
            # 品詞別の意味がない古い形式の単語は、意味の要約を「その他」の欄に入れる
            groups = [{'code': 'other', 'meanings': [word.meaning]}]
        for group in groups:
            initial[f'pos_{group["code"]}'] = True
            initial[f'meanings_{group["code"]}'] = '\n'.join(group['meanings'])
        return initial

    def clean_entry_type(self):
        return self.cleaned_data.get('entry_type') or Word.ENTRY_WORD

    def pos_fields(self):
        """テンプレートで品詞ごとにボタンと入力欄を並べるためのリスト"""
        return [
            {'code': code, 'label': label, 'toggle': self[f'pos_{code}'], 'meanings': self[f'meanings_{code}']}
            for code, label in Sense.PART_OF_SPEECH_CHOICES
        ]

    def clean_synonyms(self):
        synonyms = normalize_synonyms(self.cleaned_data.get('synonyms', ''))
        max_length = Word._meta.get_field('synonyms').max_length
        if len(synonyms) > max_length:
            raise forms.ValidationError(f'同義語は合計{max_length}文字以内で入力してください。')
        return synonyms

    def clean_phrases(self):
        """熟語を1行ずつ確認し、(表現, 意味) のリストにする。問題のある行は行番号つきでエラーにする"""
        text = self.cleaned_data.get('phrases', '')
        max_length = Phrase._meta.get_field('expression').max_length
        errors = []
        phrases = []
        for number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            expression, meaning = parse_phrase_line(line)
            has_separator = any(sep in line for sep in PHRASE_SEPARATORS)
            if not has_separator and JAPANESE_CHARACTERS.search(line):
                errors.append(f'{number}行目：「=」が見つかりません。表現と意味の間に「=」を入れてください'
                              '（例: select A from B = BからAを選ぶ）。')
            elif not expression:
                errors.append(f'{number}行目：「=」の前に表現を入力してください（例: select A from B = BからAを選ぶ）。')
            elif len(expression) > max_length or len(meaning) > max_length:
                errors.append(f'{number}行目：表現と意味は、それぞれ{max_length}文字以内で入力してください。')
            else:
                phrases.append((expression, meaning))
        if errors:
            raise forms.ValidationError(errors)
        self.cleaned_phrases = phrases
        return text

    def clean(self):
        """品詞ボタンを押した欄の意味を集める。意味が1つもなければエラーにする"""
        cleaned_data = super().clean()
        if cleaned_data.get('entry_type') == Word.ENTRY_PHRASE:
            return self.clean_phrase_entry(cleaned_data)

        max_length = Sense._meta.get_field('meaning').max_length
        senses = []
        ignored_labels = []
        for code, label in Sense.PART_OF_SPEECH_CHOICES:
            if not cleaned_data.get(f'pos_{code}'):
                # ボタンを押していない品詞の欄は保存しない。
                # ただし文字が残っていたら、気づけるように品詞名を覚えておく（登録後に警告を出す）
                if split_lines(cleaned_data.get(f'meanings_{code}', '')):
                    ignored_labels.append(label)
                continue
            for number, meaning in enumerate(split_lines(cleaned_data.get(f'meanings_{code}', '')), start=1):
                if len(meaning) > max_length:
                    self.add_error(f'meanings_{code}', f'{number}つ目の意味が長すぎます（{max_length}文字以内）。')
                else:
                    senses.append((code, meaning))

        if not senses and not self.has_error(None):
            if ignored_labels:
                self.add_error(None, f'{"・".join(ignored_labels)}の意味が入力されていますが、品詞のボタンが選ばれていません。'
                                     '保存したい品詞のボタンを押して、青くしてください。')
            else:
                self.add_error(None, '意味を1つ以上入力してください。品詞のボタンを押すと入力欄が開きます。')
        self.cleaned_senses = senses
        self.ignored_pos_labels = ignored_labels
        return cleaned_data

    def clean_phrase_entry(self, cleaned_data):
        """熟語・イディオムの意味を集める（品詞は使わず「その他」として保存する。品詞の欄は使わない）"""
        max_length = Sense._meta.get_field('meaning').max_length
        senses = []
        for number, meaning in enumerate(split_lines(cleaned_data.get('phrase_meanings', '')), start=1):
            if len(meaning) > max_length:
                self.add_error('phrase_meanings', f'{number}つ目の意味が長すぎます（{max_length}文字以内）。')
            else:
                senses.append(('other', meaning))
        if not senses and not self.has_error('phrase_meanings'):
            self.add_error('phrase_meanings', '熟語・イディオムの意味を1つ以上入力してください。')
        self.cleaned_senses = senses
        self.ignored_pos_labels = []
        return cleaned_data

    def save(self, commit=True):
        """単語本体・品詞別の意味・熟語をまとめて保存する（どれかが失敗したら全て取り消す）"""
        word = super().save(commit=False)
        word.meaning = summarize_senses(self.cleaned_senses)
        if not commit:
            return word

        with transaction.atomic():
            word.save()
            # 古い意味と熟語を消してから作り直す（段階3の編集でも同じ処理を使う）
            word.senses.all().delete()
            Sense.objects.bulk_create([
                Sense(word=word, part_of_speech=code, meaning=meaning, order=order)
                for order, (code, meaning) in enumerate(self.cleaned_senses)
            ])
            word.phrases.all().delete()
            Phrase.objects.bulk_create([
                Phrase(word=word, expression=expression, meaning=meaning, order=order)
                for order, (expression, meaning) in enumerate(self.cleaned_phrases)
            ])
        return word


class RandomReviewForm(forms.Form):
    """ランダム復習の設定（問題数と、対象にする復習レベル）"""

    COUNT_ALL = 'all'
    COUNT_CHOICES = [('10', '10問'), ('30', '30問'), (COUNT_ALL, '全問')]
    LEVEL_CHOICES = [(str(level), f'Lv.{level}') for level in range(6)]

    count = forms.ChoiceField(
        label='問題数', choices=COUNT_CHOICES, initial='10', widget=forms.RadioSelect,
    )
    levels = forms.MultipleChoiceField(
        label='復習レベル',
        choices=LEVEL_CHOICES,
        initial=[value for value, _label in LEVEL_CHOICES],  # 最初は全レベルを選んだ状態
        widget=forms.CheckboxSelectMultiple,
        error_messages={'required': '復習レベルを1つ以上選んでください。'},
    )

    def selected_levels(self):
        return [int(level) for level in self.cleaned_data['levels']]

    def selected_count(self):
        """問題数（全問なら None）"""
        count = self.cleaned_data['count']
        return None if count == self.COUNT_ALL else int(count)

from django import forms

from .models import Word


class WordForm(forms.ModelForm):
    """単語登録フォーム。Wordモデルの項目をもとに入力欄を自動で作る"""

    class Meta:
        model = Word
        fields = ['english', 'meaning', 'example']
        widgets = {
            'english': forms.TextInput(attrs={'placeholder': '例: negotiate'}),
            'meaning': forms.TextInput(attrs={'placeholder': '例: 交渉する'}),
            'example': forms.Textarea(attrs={'rows': 2, 'placeholder': '例: We need to negotiate the price.'}),
        }

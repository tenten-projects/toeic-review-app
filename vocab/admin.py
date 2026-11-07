from django.contrib import admin

from .models import Phrase, ReviewLog, Sense, Word


class SenseInline(admin.TabularInline):
    """単語の編集画面の中で、品詞別の意味も編集できるようにする"""
    model = Sense
    extra = 0


class PhraseInline(admin.TabularInline):
    """単語の編集画面の中で、熟語・関連表現も編集できるようにする"""
    model = Phrase
    extra = 0


@admin.register(Word)
class WordAdmin(admin.ModelAdmin):
    list_display = ['english', 'entry_type', 'meaning', 'review_level', 'next_review_at', 'created_at']
    list_filter = ['entry_type', 'review_level']
    search_fields = ['english', 'meaning', 'synonyms']
    inlines = [SenseInline, PhraseInline]


@admin.register(ReviewLog)
class ReviewLogAdmin(admin.ModelAdmin):
    list_display = ['word', 'is_correct', 'mode', 'reviewed_at']
    list_filter = ['is_correct', 'mode']

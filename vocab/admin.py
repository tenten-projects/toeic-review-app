from django.contrib import admin

from .models import ReviewLog, Word


@admin.register(Word)
class WordAdmin(admin.ModelAdmin):
    list_display = ['english', 'meaning', 'review_level', 'next_review_at', 'created_at']
    search_fields = ['english', 'meaning']


@admin.register(ReviewLog)
class ReviewLogAdmin(admin.ModelAdmin):
    list_display = ['word', 'is_correct', 'reviewed_at']
    list_filter = ['is_correct']

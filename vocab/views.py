from django.contrib import messages
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import dashboard as dashboard_stats
from . import scheduling
from .forms import WordForm
from .models import Word


def word_list(request):
    """単語一覧を表示し、フォームから送られた単語を登録する"""
    if request.method == 'POST':
        # 「登録」ボタンが押されたとき
        form = WordForm(request.POST)
        if form.is_valid():
            word = form.save()
            messages.success(request, f'「{word.english}」を登録しました。')
            # 登録後は一覧ページへ移動する（再読み込みでの二重登録を防ぐ）
            return redirect('vocab:word_list')
    else:
        # 普通にページを開いたとき
        form = WordForm()

    words = Word.objects.with_stats()  # 復習回数や正答率も一緒に取り出す
    return render(request, 'vocab/word_list.html', {
        'form': form,
        'words': words,
        # テンプレートで「次回予定 < 締め切り」なら今日の復習対象と表示する
        'due_cutoff': scheduling.due_cutoff(),
        # ページ上部の「今日は○語の復習があります」の表示用（復習画面と同じ数え方）
        'due_count': scheduling.due_words().count(),
    })


def word_delete(request, pk):
    """削除の確認画面を表示し、確定されたら単語を削除する"""
    word = get_object_or_404(Word, pk=pk)  # 存在しないIDなら404エラー

    if request.method == 'POST':
        word.delete()
        messages.success(request, f'「{word.english}」を削除しました。')
        return redirect('vocab:word_list')

    return render(request, 'vocab/word_confirm_delete.html', {'word': word})


def review(request):
    """今日の復習対象から、優先度が一番高い単語を1問出題する"""
    now = timezone.now()
    word = scheduling.pick_next_word(now)
    context = {
        'word': word,
        'due_count': scheduling.due_words(now).count(),
        'has_words': Word.objects.exists(),
        'upcoming_word': None,
    }
    if word is None:
        # 今日の分が終わっていたら、次に復習予定がある単語を案内する
        context['upcoming_word'] = Word.objects.order_by('next_review_at', 'pk').first()
    return render(request, 'vocab/review.html', context)


@require_POST  # POST以外（GETなど）でアクセスされたら405エラーにする
def review_answer(request, pk):
    """「覚えていた / 覚えていなかった」の結果を保存して次の問題へ進む"""
    word = get_object_or_404(Word, pk=pk)

    result = request.POST.get('result')
    if result not in ('correct', 'incorrect'):
        return HttpResponseBadRequest('result の値が正しくありません。')

    # 履歴の追加と、レベル・次回復習予定日時の更新をまとめて行う
    scheduling.record_review(word, is_correct=(result == 'correct'))

    label = '覚えていた' if result == 'correct' else '覚えていなかった'
    next_date = timezone.localtime(word.next_review_at)  # UTC → 日本時間に直して表示
    messages.success(
        request,
        f'「{word.english}」: {label} として記録しました。'
        f'（レベル{word.review_level}・次回 {next_date:%Y/%m/%d}）',
    )
    # 保存後は出題ページへ移動する（再読み込みでの重複登録を防ぐ）
    return redirect('vocab:review')


def dashboard(request):
    """学習状況（サマリー・過去7日間の復習回数・レベル別の単語数）を表示する"""
    now = timezone.now()  # 全ての集計で同じ「現在日時」を使う
    return render(request, 'vocab/dashboard.html', {
        'summary': dashboard_stats.get_summary(now),
        'daily_reviews': dashboard_stats.get_daily_reviews(now),
        'level_counts': dashboard_stats.get_level_counts(),
    })

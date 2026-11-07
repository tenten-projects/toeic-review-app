import threading
import uuid

from django.contrib import messages
from django.db.models import Count
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from . import dashboard as dashboard_stats
from . import scheduling
from .forms import RandomReviewForm, WordForm
from .models import Word


def word_list(request):
    """単語一覧を表示し、フォームから送られた単語を登録する"""
    if request.method == 'POST':
        # 「登録」ボタンが押されたとき
        form = WordForm(request.POST)
        if form.is_valid():
            word = form.save()
            messages.success(request, f'「{word.english}」を登録しました。')
            warn_ignored_parts_of_speech(request, form)
            # 登録後は一覧ページへ移動する（再読み込みでの二重登録を防ぐ）
            return redirect('vocab:word_list')
    else:
        # 普通にページを開いたとき
        form = WordForm()

    # 復習回数や正答率も一緒に取り出す。品詞別の意味と熟語は、まとめて先に読み込む（単語ごとの問い合わせを防ぐ）
    words = Word.objects.with_stats().prefetch_related('senses', 'phrases')
    return render(request, 'vocab/word_list.html', {
        'form': form,
        'words': words,
        # テンプレートで「次回予定 < 締め切り」なら今日の復習対象と表示する
        'due_cutoff': scheduling.due_cutoff(),
        # ページ上部の「今日は○語の復習があります」の表示用（復習画面と同じ数え方）
        'due_count': scheduling.due_words().count(),
    })


def warn_ignored_parts_of_speech(request, form):
    """入力したのに保存されなかった意味があることを、黙って捨てずに知らせる（登録・編集で共通）"""
    if form.ignored_pos_labels:
        messages.warning(
            request,
            f'{"・".join(form.ignored_pos_labels)}の欄に入力がありましたが、'
            '品詞のボタンが選ばれていなかったため保存していません。',
        )


def word_edit(request, pk):
    """登録済みの単語を編集する（復習レベル・次回復習日・履歴は変えない）"""
    word = get_object_or_404(Word, pk=pk)  # 存在しないIDなら404エラー

    if request.method == 'POST':
        form = WordForm(request.POST, instance=word)
        if form.is_valid():
            # フォームが扱うのは英単語・意味・熟語・同義語・例文だけなので、
            # 復習レベルや次回復習日は読み込んだときの値のまま保存される
            word = form.save()
            messages.success(request, f'「{word.english}」を更新しました。')
            warn_ignored_parts_of_speech(request, form)
            return redirect('vocab:word_list')
    else:
        form = WordForm(instance=word)

    return render(request, 'vocab/word_edit.html', {'form': form, 'word': word})


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


# 回答の「確認〜記録」を、1回に1つずつ順番に行うための鍵（今日の復習・ランダム復習で共通）。
# ダブルクリックで2つの送信がほぼ同時に届いても、2つ目は1つ目の記録後の状態を見て「回答済み」と判断できる。
# ※ 1つのプロセスで動かす前提（runserver など）のシンプルな方法
ANSWER_LOCK = threading.Lock()


@require_POST  # POST以外（GETなど）でアクセスされたら405エラーにする
def review_answer(request, pk):
    """「覚えていた / 覚えていなかった」の結果を保存して次の問題へ進む

    二重記録を防ぐため、次の2つを満たす回答だけを記録する。
      1. 画面を表示したときの「次回復習予定日時」（フォームで一緒に送る）が、今の値と同じ
         → 回答すると予定日時は必ず変わるので、ダブルクリックの2回目・「戻る」で開いた古い画面・
           前日から開いたままの画面からの送信は、ここで見分けられる
      2. 今日の復習対象である（今日の復習画面に出ない単語には回答できない）
    """
    result = request.POST.get('result')
    if result not in ('correct', 'incorrect'):
        return HttpResponseBadRequest('result の値が正しくありません。')

    with ANSWER_LOCK:
        word = get_object_or_404(Word, pk=pk)  # 鍵をかけてから読むので、直前の記録が反映された値になる
        now = timezone.now()
        shown_at = parse_datetime(request.POST.get('scheduled_at', ''))
        if shown_at is None or shown_at != word.next_review_at:
            messages.warning(
                request,
                f'「{word.english}」への回答は、すでに記録済みか古い画面から送られたため、記録しませんでした。',
            )
            return redirect('vocab:review')
        if word.next_review_at >= scheduling.due_cutoff(now):
            messages.warning(request, f'「{word.english}」は今日の復習対象ではないため、記録しませんでした。')
            return redirect('vocab:review')

        # 履歴の追加と、レベル・次回復習予定日時の更新をまとめて行う
        scheduling.record_review(word, is_correct=(result == 'correct'), now=now)

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


# ---------------------------------------------------------------------------
# ランダム復習
#
# 1セットの状態（出題する単語IDの並び・今何問目か・正解数など）はセッションに保存する。
# 回答フォームには「セットのID」と「何問目か」を入れて送り、今の状態と一致したときだけ記録する。
# これで、再読み込み・二重送信・古い画面からの送信で、同じ回答が2回記録されるのを防ぐ。
# ---------------------------------------------------------------------------

RANDOM_SESSION_KEY = 'random_review'


def random_setup(request):
    """ランダム復習の設定画面（問題数とレベルを選んで開始する）"""
    if request.method == 'POST':
        form = RandomReviewForm(request.POST)
        if form.is_valid():
            word_ids = scheduling.pick_random_words(form.selected_levels(), form.selected_count())
            if word_ids:
                request.session[RANDOM_SESSION_KEY] = {
                    'set_id': uuid.uuid4().hex,  # このセットを見分けるための使い捨てのID
                    'word_ids': word_ids,
                    'position': 0,  # 次に出題する問題の位置（0から数える）
                    'is_all': form.selected_count() is None,
                    'correct': 0,
                    'incorrect': 0,
                    'skipped': 0,  # 途中で削除されて出題できなかった単語の数
                }
                return redirect('vocab:random_question')
            form.add_error(None, '選んだ復習レベルの単語・熟語がありません。別のレベルを選んでください。')
    else:
        form = RandomReviewForm()

    # レベルごとの単語数（チェックボックスの横に表示する）
    counts = dict(Word.objects.order_by().values_list('review_level').annotate(total=Count('pk')))
    level_options = [(option, counts.get(int(option.data['value']), 0)) for option in form['levels']]
    return render(request, 'vocab/random_setup.html', {
        'form': form,
        'level_options': level_options,
        'word_count': sum(counts.values()),
        'weights': scheduling.RANDOM_WEIGHTS,
    })


def _current_random_set(request):
    return request.session.get(RANDOM_SESSION_KEY)


def _save_random_set(request, state):
    # 辞書の中身だけを書き換えても保存されないので、入れ直して「変更あり」にする
    request.session[RANDOM_SESSION_KEY] = state


def random_question(request):
    """ランダム復習の問題を1問表示する"""
    state = _current_random_set(request)
    if state is None:
        messages.info(request, 'ランダム復習の問題数とレベルを選んでください。')
        return redirect('vocab:random_setup')

    # 途中で削除された単語は飛ばす
    word = None
    while state['position'] < len(state['word_ids']):
        word = Word.objects.prefetch_related('senses', 'phrases').filter(pk=state['word_ids'][state['position']]).first()
        if word is not None:
            break
        state['skipped'] += 1
        state['position'] += 1
    _save_random_set(request, state)

    if word is None:
        return redirect('vocab:random_result')

    return render(request, 'vocab/random_review.html', {
        'word': word,
        'state': state,
        'number': state['position'] + 1,
        'total': len(state['word_ids']),
        'is_due': word.next_review_at < scheduling.due_cutoff(),
    })


@require_POST
def random_answer(request):
    """ランダム復習の回答を記録して、次の問題へ進む"""
    # 今日の復習と同じ鍵を使い、確認〜記録〜セッション保存を1回に1つずつ行う
    with ANSWER_LOCK:
        response = _record_random_answer(request)
        request.session.save()  # 鍵を開ける前に、進んだ位置を保存しておく
    return response


def _record_random_answer(request):
    result = request.POST.get('result')
    if result not in ('correct', 'incorrect'):
        return HttpResponseBadRequest('result の値が正しくありません。')

    state = _current_random_set(request)
    if state is None or request.POST.get('set_id') != state['set_id']:
        # 終わったセットや、別のセットの画面から送られた回答は記録しない
        messages.warning(request, 'この回答は古い画面から送られたため、記録しませんでした。')
        return redirect('vocab:random_question' if state else 'vocab:random_setup')

    position = state['position']
    finished = position >= len(state['word_ids'])
    if (finished or request.POST.get('position') != str(position)
            or request.POST.get('word_id') != str(state['word_ids'][position])):
        # 再読み込みや二重送信で、回答済みの問題がもう一度送られてきた
        messages.warning(request, 'この問題はすでに回答済みです。記録は1回だけ行われています。')
        return redirect('vocab:random_question')

    word = Word.objects.filter(pk=state['word_ids'][position]).first()
    state['position'] += 1
    if word is None:
        # 答えている間に、別の画面で単語が削除された
        state['skipped'] += 1
        _save_random_set(request, state)
        messages.warning(request, '出題中の単語・熟語は削除されたため、記録しませんでした。')
        return redirect('vocab:random_question')

    is_correct = result == 'correct'
    updated = scheduling.record_random_review(word, is_correct)
    state['correct' if is_correct else 'incorrect'] += 1
    _save_random_set(request, state)

    label = '覚えていた' if is_correct else '覚えていなかった'
    if updated:
        next_date = timezone.localtime(word.next_review_at)
        detail = f'レベル{word.review_level}・次回 {next_date:%Y/%m/%d}'
    else:
        detail = '復習予定日より前なので、レベルと次回復習日は変わりません'
    messages.success(request, f'「{word.english}」: {label} として記録しました。（{detail}）')
    return redirect('vocab:random_question')


def random_result(request):
    """ランダム復習1セットの結果を表示する"""
    state = _current_random_set(request)
    if state is None:
        return redirect('vocab:random_setup')
    if state['position'] < len(state['word_ids']):
        return redirect('vocab:random_question')  # まだ途中なら問題に戻す

    answered = state['correct'] + state['incorrect']
    return render(request, 'vocab/random_result.html', {
        'state': state,
        'answered': answered,
        'accuracy': dashboard_stats.calc_accuracy(state['correct'], answered),
    })

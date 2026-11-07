from django.urls import path

from . import views

app_name = 'vocab'

urlpatterns = [
    path('', views.word_list, name='word_list'),
    path('words/<int:pk>/edit/', views.word_edit, name='word_edit'),
    path('words/<int:pk>/delete/', views.word_delete, name='word_delete'),
    path('review/', views.review, name='review'),
    path('review/<int:pk>/answer/', views.review_answer, name='review_answer'),
    path('random/', views.random_setup, name='random_setup'),
    path('random/question/', views.random_question, name='random_question'),
    path('random/answer/', views.random_answer, name='random_answer'),
    path('random/result/', views.random_result, name='random_result'),
    path('dashboard/', views.dashboard, name='dashboard'),
]

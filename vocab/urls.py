from django.urls import path

from . import views

app_name = 'vocab'

urlpatterns = [
    path('', views.word_list, name='word_list'),
    path('words/<int:pk>/delete/', views.word_delete, name='word_delete'),
    path('review/', views.review, name='review'),
    path('review/<int:pk>/answer/', views.review_answer, name='review_answer'),
    path('dashboard/', views.dashboard, name='dashboard'),
]

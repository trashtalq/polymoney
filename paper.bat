@echo off
title POLYMONEY - TENEVOY BOT (PAPER $15k)
rem Теневой бот: та же логика/задержки/кэфы, что реал, но виртуальный банк $15k БЕЗ денег и ключа.
rem Запускай ВТОРЫМ процессом рядом с реальным ботом. Каждый цикл печатает [PAPER] банк и PnL.
set MODE=paper
python live_executor.py

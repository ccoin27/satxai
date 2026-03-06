#!/bin/bash
# Создание кастомной модели satx-host из Modelfile
# Требуется: ollama pull hermes3:8b (если ещё не скачан)

ollama pull hermes3:8b
ollama create satx-host -f Modelfile
echo "Модель satx-host готова. Запусти: python main.py"

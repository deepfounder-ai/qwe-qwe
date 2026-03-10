# Qdrant Research Summary

## Что такое Qdrant?
**Qdrant** — это векторная база данных (vector database) для хранения и поиска по эмбеддингам. Разрабатывается компанией Qdrant GmbH (Берлин, Германия).

## Основные возможности

### 1. Векторный поиск
- **Гибридный поиск**: vector + scalar filters
- Поддержка различных метрик: cosine, dot product, euclidean, manhattan и др.
- HNSW индекс для быстрого поиска в больших коллекциях

### 2. Функции
- **Коллекции**: логические группы векторов
- **Векторы**: эмбеддинги текстов, изображений, аудио
- **Метаданные**: фильтры по ключам/значениям
- **Пункты (points)**: единицы хранения

### 3. Интеграции
- REST API
- Python SDK (`qdrant-client`)
- gRPC
- Коннекторы для MLфреймворков
- Поддержка Docker/Kubernetes

## Установка

```bash
# Через pip
pip install qdrant-client

# Локальный сервер (Docker)
docker run -p 6333:6333 -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant
```

## Пример использования

```python
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, PointStruct

client = QdrantClient(host="localhost", port=6333)

# Создаёт коллекцию
client.recreate_collection(
    collection_name="documents",
    vectors_config=VectorParams(size=1536, distance="Cosine")  # эмбеддинги BERT/All-MiniLM
)

# Добавляет векторы
client.upsert(
    collection_name="documents",
    points=[PointStruct(id=1, vector=[0.1]*1536, payload={"text": "Привет"})]
)

# Поиск по вектору
result = client.search(
    collection_name="documents",
    query_vector=[0.2]*1536,
    limit=3
)
```

## Архитектура

```
┌─────────────────┐
│   Qdrant Server  │
├─────────────────┤
│ • HNSW индекс    │
│ • Фильтры        │
│ • Векторы+payload│
└─────────────────┘
         ↓
┌─────────────────┐
│   REST API / gRPC│
└─────────────────┘
         ↓
┌─────────────────┐
│  Python SDK      │
└─────────────────┘
```

## Сравнение с альтернативами

| База данных | Тип | Плюсы | Минусы |
|-------------|-----|-------|--------|
| **Qdrant** | Vector DB | Быстрый HNSW, фильтры, легковесный | Платформозависимый |
| **Pinecone** | Managed | Полное управление | Cloud-only, дорого |
| **Milvus** | Vector DB | Масштабируемость | Сложная настройка |
| **Weaviate** | Vector+KB | RAG интеграция | Python SDK слабее |

## Когда использовать Qdrant?

✅ Рекомендуется для:
- Чат-ботов с памятью (RAG)
- Поиск по изображениям/аудио
- Рекомендательные системы
- Проекты, где нужна гибкость фильтров

❌ Не подходит:
- Только простые текстовые поиски
- Нужен managed service без самохостинга

## Документация и ресурсы

- **Официальная**: https://qdrant.tech/documentation/
- **GitHub**: https://github.com/qdrant/qdrant
- **Docker Hub**: qdrant/qdrant
- **Discord**: https://qdrant.to/discord

---
*Исследование выполнено 2024. Qdrant — лидер в open-source векторных БД.*

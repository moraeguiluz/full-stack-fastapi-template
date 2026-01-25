import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.seed.news_seed import seed_news
from app.snippets.news import News


def main() -> None:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is required")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)

    engine = create_engine(url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with SessionLocal() as db:
        result = seed_news(db, News)

    print(
        f"Seeded news: {result.created} created, {result.skipped} skipped, {result.total} total"
    )


if __name__ == "__main__":
    main()

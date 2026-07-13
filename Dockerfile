# Образ API. Несёт внутри тулчейн Catala — не опционально: компиляция тарифа
# происходит в рантайме, на шаге Validate (application/tariff_validation.py),
# а CatalaFormulaEngine при промахе дискового кэша пересобирает артефакт из
# catala_source, лежащего в БД. Без компилятора сервис поднимется, но любой
# POST /tariffs/.../validate упадёт.
#
# Питон — строго 3.14 (см. requires-python в pyproject.toml): на 3.12/3.13
# сгенерированный Catala stdlib ловит циклический импорт.
#
# Тулчейн ставится тем же рецептом, что в catala_research/Dockerfile —
# switch обязан называться "catala": infrastructure/formula_engine/
# catala_toolchain.py:_opam_prefix() зовёт `opam var --switch=catala prefix`,
# то есть в рантайме нужен сам opam, а не только бинарь catala в PATH.
FROM python:3.14-slim

ENV DEBIAN_FRONTEND=noninteractive \
    OPAMROOT=/opt/opam \
    OPAMYES=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        opam \
        m4 \
        pkg-config \
        libgmp-dev \
        build-essential \
        ninja-build \
        ca-certificates \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# --disable-sandboxing: bwrap-песочница opam не работает во вложенных
# user namespaces контейнера; сборка и так изолирована самим контейнером.
RUN opam init --bare --disable-sandboxing -a \
    && opam switch create catala 4.14.2 \
    && opam install -y --switch=catala catala.1.2.1 \
    && opam clean --switch=catala -a -c -s -r

ENV PATH="/opt/opam/catala/bin:${PATH}"

WORKDIR /app

# Зависимости отдельным слоем — не пересобираются при правке кода.
COPY pyproject.toml uv.lock README.md* ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir "pytest>=8.3" "httpx>=0.27"  # dependency-groups.dev

# Тесты едут в образ, чтобы прогонять их против ТОГО ЖЕ артефакта, который
# деплоится (реальный Postgres, реальный компилятор Catala):
#   docker compose -f docker-compose.prod.yml run --rm api pytest
COPY tests ./tests

# Дисковый кэш тулчейна (stdlib + собранные артефакты). Держим ВНУТРИ образа,
# а не в томе: это чистый кэш, воспроизводимый из catala_source в БД, и
# прогрев на этапе сборки убирает 30-секундную паузу на первом же Validate.
ENV BILLING_CATALA_BUILD_DIR=/build/catala
RUN mkdir -p /build/catala && python -c "\
from billing.infrastructure.formula_engine.catala_toolchain import _stdlib_python_cache, compile_source; \
from billing.infrastructure.formula_engine.fixtures import load_source; \
_stdlib_python_cache(); \
print('stdlib cache warmed'); \
a = compile_source(load_source('comfort_v1')); \
print('artifact warmed:', a.source_hash[:12])"

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Непривилегированный пользователь; /build должен остаться писабельным —
# при появлении НОВОГО тарифа артефакт для него соберётся в рантайме.
RUN useradd -m -u 1000 app && chown -R app:app /build
USER app

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "billing.interface.http.app:app", "--host", "0.0.0.0", "--port", "8000"]

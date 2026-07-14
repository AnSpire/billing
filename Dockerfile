# Образ API. Тулчейн Catala приезжает из базового образа (Dockerfile.toolchain)
# — он не опционален: компиляция тарифа происходит в рантайме, на шаге Validate
# (application/tariff_validation.py), а CatalaFormulaEngine при промахе дискового
# кэша пересобирает артефакт из catala_source, лежащего в БД. Без компилятора
# сервис поднимется, но любой POST /tariffs/.../validate упадёт.
#
# Базовый образ собирается отдельно и редко (~40 минут, только при смене версии
# Catala):
#
#   make toolchain-image
#
# Его нет в реестре, он живёт локально в демоне — поэтому на новой машине (и под
# другим демоном: rootless vs sudo) сначала `make toolchain-image`, потом уже
# `docker compose -f docker-compose.prod.yml up -d --build`. Если базового образа
# нет, сборка упадёт сразу с "pull access denied" — это ожидаемо, а не поломка.
ARG TOOLCHAIN_IMAGE=billing-catala-toolchain:1.2.1
FROM ${TOOLCHAIN_IMAGE}

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

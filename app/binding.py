"""Пара (коллекция, модель), которой отвечает сервис поиска, и её смена по алиасу Qdrant.

Запрос берёт снимок состояния один раз и до конца работает с одной парой: вектор вопроса считает
модель пары, ищет он в коллекции пары. Пара меняется целиком, одним присваиванием, поэтому запрос
не может взять модель от одной пары, а коллекцию от другой.

С алиасом (SEARCH_ALIAS) фоновый поток раз в ttl секунд смотрит, куда алиас указывает (resolve).
Новую цель он берёт только после проверки (verify): коллекция есть в реестре «коллекция → модель»,
и её отпечаток сошёлся с моделью из реестра. Не сошлось — отказ: сервис отвечает прежней парой,
отказ виден в состоянии (view: оно уходит в /readyz и в поле model_check ответа) и пишется
в журнал; ту же цель поток проверяет снова не чаще раза в recheck секунд. Проверка идёт в фоне,
поэтому запросы пользователей её не ждут, даже если модели нужно время, чтобы загрузиться.
Пару на старте (COLLECTION, EMBED_MODEL) поток тоже проверяет. Если реестр её не знает, сервис
отвечает ею, как отвечал до реестра, с пометкой unverified.

Без сети и без FastAPI: resolve и verify передаёт app/api.py, тесты подставляют свои.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable


def registry_id(collection: str) -> str:
    """id записи коллекции в реестре. То же считает deploy/qdrant/registry.py, совпадение сверяет тест."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "embedding-registry/" + collection))


def cosine(a, b) -> float:
    if len(a) != len(b):
        raise ValueError(f"vectors of different length: {len(a)} and {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class Pair:
    collection: str
    model: str
    verified: bool = False
    cosine: float | None = None


@dataclass(frozen=True)
class Refusal:
    collection: str
    reason: str
    model: str | None = None  # модель, которой цель подписана в реестре, если запись есть


@dataclass(frozen=True)
class State:
    pair: Pair
    target: str | None = None       # куда указывал алиас при последнем чтении (None: алиаса нет)
    resolved: bool = False          # алиас хоть раз прочитан; до этого под не готов
    refused: Refusal | None = None  # отказ цели, пока алиас указывает на неё
    note: str | None = None         # почему пара на старте не проверена


class Refused(Exception):
    """Цель не прошла проверку. Причина по-английски: она уходит клиенту в model_check и в журнал."""

    def __init__(self, reason: str, model: str | None = None) -> None:
        super().__init__(reason)
        self.reason, self.model = reason, model


def _short(e: BaseException) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text}"[:200] if text else type(e).__name__


class Binding:
    def __init__(self, alias: str, collection: str, model: str, *, resolve: Callable[[], str | None],
                 verify: Callable[[str], Pair], ttl: float = 1.0, recheck: float = 5.0,
                 log: Callable[..., None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.alias = alias
        self.resolve, self.verify = resolve, verify
        self.ttl, self.recheck = ttl, recheck
        self.log = log or (lambda event, **kw: None)
        self.clock = clock
        self.state = State(Pair(collection, model))
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tried: dict[str, float] = {}       # коллекция -> когда проверяли последний раз
        self._said: dict[str, object] = {}        # что уже записано в журнал: не повторять каждую секунду

    # --- один шаг ------------------------------------------------------------------------------
    def refresh(self) -> State:
        """Прочитать алиас; при новой цели проверить её и, если сошлось, сменить пару."""
        with self._lock:
            now = self.clock()
            try:
                target = self.resolve()
            except Exception as e:  # Qdrant не ответил: остаёмся на том, что есть
                self._say("alias", "alias_unreadable", _short(e), alias=self.alias, error=_short(e))
                return self.state
            self._said.pop("alias", None)
            st = replace(self.state, target=target, resolved=True)
            if not st.pair.verified and self._due(st.pair.collection, now):
                st = self._check_start(st, now)
            if target is not None and target != st.pair.collection:
                if st.refused is None or st.refused.collection != target or self._due(target, now):
                    st = self._check_target(st, target, now)
            elif st.refused is not None:  # алиас ушёл с отказанной цели
                st = replace(st, refused=None)
                self._said.pop("target", None)
            self.state = st
            return st

    def _due(self, collection: str, now: float) -> bool:
        last = self._tried.get(collection)
        return last is None or now - last >= self.recheck

    def _check_start(self, st: State, now: float) -> State:
        self._tried[st.pair.collection] = now
        try:
            pair = self.verify(st.pair.collection)
        except Refused as e:
            reason = e.reason
        except Exception as e:
            reason = f"cannot verify: {_short(e)}"
        else:
            self._said.pop("start", None)
            self.log("model_verified", collection=pair.collection, model=pair.model, cosine=pair.cosine,
                     configured_model=st.pair.model)
            return replace(st, pair=pair, note=None)
        self._say("start", "model_unverified", reason, collection=st.pair.collection, model=st.pair.model,
                  reason=reason)
        return replace(st, note=reason)

    def _check_target(self, st: State, target: str, now: float) -> State:
        self._tried[target] = now
        t0 = self.clock()
        try:
            pair = self.verify(target)
        except Refused as e:
            refusal = Refusal(target, e.reason, e.model)
        except Exception as e:  # Qdrant или векторизатор не ответили: проверим ещё раз через recheck
            refusal = Refusal(target, f"cannot verify: {_short(e)}")
        else:
            self._said.pop("target", None)
            self.log("model_switch", alias=self.alias,
                     **{"from": {"collection": st.pair.collection, "model": st.pair.model},
                        "to": {"collection": pair.collection, "model": pair.model}},
                     cosine=pair.cosine, check_ms=round((self.clock() - t0) * 1000, 1))
            return replace(st, pair=pair, refused=None, note=None)
        self._say("target", "model_refused", (refusal.collection, refusal.reason), alias=self.alias,
                  target=target, target_model=refusal.model, reason=refusal.reason,
                  serving={"collection": st.pair.collection, "model": st.pair.model})
        return replace(st, refused=refusal)

    def _say(self, slot: str, event: str, what: object, **fields) -> None:
        """В журнал только перемены: повторный отказ той же цели по той же причине не пишется."""
        if self._said.get(slot) != what:
            self._said[slot] = what
            self.log(event, **fields)

    # --- снаружи ------------------------------------------------------------------------------
    def view(self, st: State | None = None) -> dict:
        """Состояние проверки для клиента (поле model_check) и для /readyz."""
        st = st or self.state
        p = st.pair
        if st.target is not None and st.target != p.collection and st.refused is not None:
            status = "refused"
        else:
            status = "verified" if p.verified else "unverified"
        out = {"status": status, "alias": self.alias, "target": st.target,
               "verified": p.verified, "cosine": p.cosine}
        if status == "refused":
            out["target_model"] = st.refused.model
            out["reason"] = st.refused.reason
        elif status == "unverified" and st.note:
            out["reason"] = st.note
        return out

    def ensure_running(self) -> None:
        """Фоновый поток: refresh раз в ttl секунд. Повторный вызов ничего не делает."""
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None:
                t = threading.Thread(target=self._loop, name="alias-watch", daemon=True)
                t.start()
                self._thread = t

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as e:  # поток не должен умереть ни от чего
                self._say("loop", "alias_watch_error", _short(e), error=_short(e))
            self._stop.wait(self.ttl)

    def stop(self) -> None:
        """Остановить фоновый поток (тесты); в поде он живёт, пока жив процесс."""
        self._stop.set()

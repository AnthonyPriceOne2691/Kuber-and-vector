"""Манифесты стенда для проверок в CI (kubeconform, kube-linter): копии с подставленными адресами.
На стенде адреса подставляют install-скрипты; здесь берутся служебные значения из документации
(192.0.2.0/24 для примеров) и адреса сервисов из сети сервисов k3s. Оставшийся шаблон это ошибка:
значит, появился новый, а CI про него не знает.
  python3 -I -B deploy/ci/render.py ПАПКА
Берутся файлы deploy/**/*.yaml, в которых есть строки «apiVersion:» и «kind:» с начала строки;
values чартов и конфиги Prometheus и Grafana под это не попадают.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
VALUES = {
    "ENTRY_IP": "192.0.2.10",
    "QDRANT_CLUSTER_IP": "10.43.0.50",
    "QDRANT_LOCAL_CLUSTER_IP": "10.43.0.51",
    "@ALLOWED@": "['localhost/search:stage2', 'docker.io/qdrant/qdrant:v1.20.2', 'docker.io/falcosecurity/falco:0.45.2']",
    "@ACTIONS@": "[Deny]",
}
LEFTOVER = re.compile(r"@[A-Z_]+@|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_IP\b")


def manifests(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    found = []
    for path in sorted((root / "deploy").rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?m)^apiVersion:", text) and re.search(r"(?m)^kind:", text):
            found.append(path)
    return found


def render(text: str) -> str:
    for key, value in VALUES.items():
        pattern = re.escape(key) if key.startswith("@") else rf"\b{re.escape(key)}\b"
        text = re.sub(pattern, value, text)
    body = "\n".join(line.split("#", 1)[0] for line in text.splitlines())  # в комментариях шаблоны можно называть
    left = sorted(set(LEFTOVER.findall(body)))
    if left:
        raise ValueError(f"неизвестные шаблоны: {', '.join(left)}")
    return text


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    out = pathlib.Path(argv[0])
    files = manifests()
    for path in files:
        rel = path.relative_to(ROOT / "deploy")
        try:
            text = render(path.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"{rel}: {e}", file=sys.stderr)
            return 1
        # одна плоская папка: kube-linter сопоставляет сервисы и поды только внутри одной папки
        dest = out / "-".join(rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(rel)
    print(f"манифестов: {len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Подписи издателей для комплекта стенда

Проверено 18.11.2026 22:31 в одноразовой ВМ (`deploy/vm/kit-verify.sh`).

| Компонент | Чем проверено | Итог |
|---|---|---|
| cosign v3.1.4 (инструмент проверки) | sha256 из его выпуска на GitHub | сошлось; пакета в Ubuntu нет, независимой опоры нет |
| Образ Ubuntu 24.04, release-20261017 | GPG, ключ из пакета ubuntu-cloudimage-keyring | подпись верна; закреплённый sha256 есть в подписанном списке |
| Lima v2.3.0 (на Маке) | GPG, ключ сопровождающего с github.com/AkihiroSuda.gpg | подпись верна; ключ C020EA876CE4E06C7AB95AEF49524C6F9F638F1A; на keyserver.ubuntu.com: нет |
| helm v4.3.2 | GPG, ключ с keyserver по отпечатку из заметок к выпуску | подпись верна; ключ совпал с отпечатком |
| trivy v0.76.2 | Sigstore, подпись выпускного workflow aquasecurity/trivy по тегу v0.76.2 | подпись верна; tar.gz сверен с этим списком сумм |
| образ docker.io/qdrant/qdrant | Sigstore (cosign verify) | подписан: "Subject":"https://github.com/qdrant/qdrant/.github/workflows/docker-image.yml@refs/tags/v1.20.2" |
| образ docker.io/library/python | Sigstore (cosign verify) | подписей издатель не публикует (no signatures found) |
| чарт Qdrant 1.20.2 | provenance (.prov) | издатель не публикует; sha256 сверен с index.yaml при сборке |
| k3s v1.37.2+k3s1 | в выпуске только sha256sum | подписей издатель не публикует; sha256 сошёлся при сборке |
| kube-bench v0.16.0 | в выпуске только checksums.txt | подписей издатель не публикует; sha256 сошёлся при сборке |
| пакеты Ubuntu (haproxy, buildah, nmap) | подпись архива Ubuntu | проверил apt при загрузке |
| пакеты Python | индекс PyPI | sha256 из индекса проверил pip |
| LegalBench-RAG | источник не подписывает и сумм не публикует | sha256 зафиксирован при загрузке |

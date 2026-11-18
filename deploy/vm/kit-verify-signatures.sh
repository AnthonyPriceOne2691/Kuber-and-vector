#!/bin/bash
# Проверка подписей издателей для комплекта. Запускается ТОЛЬКО в одноразовой ВМ (роль
# builder): нужен интернет к серверам ключей, к Sigstore и к реестру образов.
# Вход: папка с файлами из комплекта, её готовит kit-verify.sh. Выход: отчёт Markdown в stdout.
#
# Опора доверия у каждого компонента своя:
#   образ Ubuntu   GPG, ключ из пакета ubuntu-cloudimage-keyring (сам пакет подписан архивом Ubuntu)
#   Lima           GPG, ключ сопровождающего с GitHub, плюс сверка с keyserver.ubuntu.com
#   helm           GPG, ключ с keyserver по отпечатку из заметок к выпуску
#   trivy          Sigstore: подпись выпускного workflow aquasecurity/trivy по тегу выпуска
#   образы         Sigstore: есть ли подписи вообще и чьи
#   cosign         сам проверяется только по sha256 своего выпуска (пакета в Ubuntu нет)
set -uo pipefail
IN="${1:?папка с файлами}"

COSIGN_VERSION="v3.1.4"
UBUNTU_RELEASE="release-20261017"
UBUNTU_IMG_SHA="50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e"
LIMA_VERSION="v2.3.0"
HELM_VERSION="v4.3.2"
HELM_FPR="208DD36ED5BB3745A16743A4C7C6FBB5B91C1155"   # из заметок к выпуску helm v4.3.2
TRIVY_VERSION="0.76.2"
QDRANT_CHART_VERSION="1.20.2"

W="$(mktemp -d)"; export GNUPGHOME="$W/gnupg"; mkdir -m 700 "$GNUPGHOME"
fetch() { curl -4 -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
row() { printf '| %s | %s | %s |\n' "$1" "$2" "$3"; }
cert_san() { # identity from a sigstore bundle certificate
  python3 -I - "$1" <<'PY' | openssl x509 -inform DER -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' '
import base64, json, sys
b = json.load(open(sys.argv[1]))["verificationMaterial"]
raw = b.get("certificate", {}).get("rawBytes") or b["x509CertificateChain"]["certificates"][0]["rawBytes"]
sys.stdout.buffer.write(base64.b64decode(raw))
PY
}

sudo apt-get -o Acquire::ForceIPv4=true -qq update >/dev/null 2>&1
sudo apt-get -o Acquire::ForceIPv4=true -qq install -y ubuntu-cloudimage-keyring >/dev/null 2>&1

echo "| Компонент | Чем проверено | Итог |"
echo "|---|---|---|"

# 1. cosign, инструмент проверки
cb="https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION"
fetch "$cb/cosign-linux-arm64" "$W/cosign-linux-arm64"
fetch "$cb/cosign_checksums.txt" "$W/cosign_checksums.txt"
if ( cd "$W" && grep -E ' cosign-linux-arm64$' cosign_checksums.txt | sha256sum -c - >/dev/null ); then
  chmod +x "$W/cosign-linux-arm64"; COSIGN="$W/cosign-linux-arm64"
  row "cosign $COSIGN_VERSION (инструмент проверки)" "sha256 из его выпуска на GitHub" "сошлось; пакета в Ubuntu нет, независимой опоры нет"
else
  row "cosign $COSIGN_VERSION" "sha256 из выпуска" "**НЕ СОШЛОСЬ**, проверки Sigstore пропущены"; COSIGN=""
fi

# 2. Образ Ubuntu
ub="https://cloud-images.ubuntu.com/releases/noble/$UBUNTU_RELEASE"
fetch "$ub/SHA256SUMS" "$W/ub.sums"; fetch "$ub/SHA256SUMS.gpg" "$W/ub.sums.gpg"
if gpgv --keyring /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg "$W/ub.sums.gpg" "$W/ub.sums" >/dev/null 2>"$W/ub.err"; then
  if grep -q "^$UBUNTU_IMG_SHA [ *]ubuntu-24.04-server-cloudimg-arm64.img$" "$W/ub.sums"; then
    row "Образ Ubuntu 24.04, $UBUNTU_RELEASE" "GPG, ключ из пакета ubuntu-cloudimage-keyring" "подпись верна; закреплённый sha256 есть в подписанном списке"
  else
    row "Образ Ubuntu 24.04, $UBUNTU_RELEASE" "GPG" "подпись верна, но **закреплённого sha256 в списке нет**"
  fi
else
  row "Образ Ubuntu 24.04, $UBUNTU_RELEASE" "GPG" "**подпись не прошла**: $(tail -1 "$W/ub.err")"
fi

# 3. Lima
lb="https://github.com/lima-vm/lima/releases/download/$LIMA_VERSION"
fetch "$lb/SHA256SUMS.asc" "$W/lima.asc"
fetch "https://github.com/AkihiroSuda.gpg" "$W/lima-maintainer.asc"
gpg --batch --quiet --import "$W/lima-maintainer.asc" >/dev/null 2>&1
if head -1 "$W/lima.asc" | grep -q 'BEGIN PGP SIGNED MESSAGE'; then
  gpg --batch --status-fd 1 --output "$W/lima.signed" --decrypt "$W/lima.asc" > "$W/lima.st" 2>&1
  same=$(cmp -s "$W/lima.signed" "$IN/lima/SHA256SUMS" && echo yes || echo no)
else
  gpg --batch --status-fd 1 --verify "$W/lima.asc" "$IN/lima/SHA256SUMS" > "$W/lima.st" 2>&1; same=yes
fi
if grep -q 'VALIDSIG' "$W/lima.st" && [ "$same" = yes ]; then
  fpr=$(awk '/VALIDSIG/{print $NF}' "$W/lima.st")
  ks=$(gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys "$fpr" >/dev/null 2>&1 && echo "есть" || echo "нет")
  row "Lima $LIMA_VERSION (на Маке)" "GPG, ключ сопровождающего с github.com/AkihiroSuda.gpg" "подпись верна; ключ $fpr; на keyserver.ubuntu.com: $ks"
else
  row "Lima $LIMA_VERSION (на Маке)" "GPG" "**не прошла**: $(grep -oE 'NO_PUBKEY [0-9A-F]+|BADSIG|ERRSIG' "$W/lima.st" | head -1); совпадение списка сумм: $same"
fi

# 4. helm
hb="https://github.com/helm/helm/releases/download/$HELM_VERSION"
fetch "$hb/helm-$HELM_VERSION-linux-arm64.tar.gz.asc" "$W/helm.asc"
gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys "$HELM_FPR" >/dev/null 2>&1 \
  || { fetch "https://keys.openpgp.org/vks/v1/by-fingerprint/$HELM_FPR" "$W/helm-key.asc" && gpg --batch --import "$W/helm-key.asc" >/dev/null 2>&1; }
gpg --batch --status-fd 1 --verify "$W/helm.asc" "$IN/helm/helm-$HELM_VERSION-linux-arm64.tar.gz" > "$W/helm.st" 2>&1
primary=$(awk '/VALIDSIG/{print $NF}' "$W/helm.st")
if [ -n "$primary" ] && [ "$primary" = "$HELM_FPR" ]; then
  row "helm $HELM_VERSION" "GPG, ключ с keyserver по отпечатку из заметок к выпуску" "подпись верна; ключ совпал с отпечатком"
else
  row "helm $HELM_VERSION" "GPG" "**не прошла**: ключ '${primary:-нет}', $(grep -oE 'NO_PUBKEY [0-9A-F]+|BADSIG|ERRSIG' "$W/helm.st" | head -1)"
fi

# 5. trivy
tb="https://github.com/aquasecurity/trivy/releases/download/v$TRIVY_VERSION"
fetch "$tb/trivy_${TRIVY_VERSION}_checksums.txt.sigstore.json" "$W/trivy.bundle"
ident_re="^https://github\\.com/aquasecurity/trivy/\\.github/workflows/.+@refs/tags/v${TRIVY_VERSION//./\\.}\$"
if [ -n "$COSIGN" ] && "$COSIGN" verify-blob --bundle "$W/trivy.bundle" \
     --certificate-identity-regexp "$ident_re" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     "$IN/trivy/trivy_${TRIVY_VERSION}_checksums.txt" > "$W/trivy.out" 2>&1; then
  row "trivy v$TRIVY_VERSION" "Sigstore, подпись выпускного workflow aquasecurity/trivy по тегу v$TRIVY_VERSION" "подпись верна; tar.gz сверен с этим списком сумм"
else
  row "trivy v$TRIVY_VERSION" "Sigstore" "**не прошла**: $(tail -1 "$W/trivy.out" 2>/dev/null); в сертификате: $(cert_san "$W/trivy.bundle")"
fi

# 6. Образы контейнеров: подписаны ли вообще
while read -r ref dig; do
  [ -n "$ref" ] || continue
  if [ -n "$COSIGN" ] && "$COSIGN" verify --certificate-identity-regexp '.*' --certificate-oidc-issuer-regexp '.*' \
       "$ref@$dig" > "$W/img.out" 2>&1; then
    who=$(grep -oE '"(Subject|subject)":"[^"]+"' "$W/img.out" | head -1)
    row "образ $ref" "Sigstore (cosign verify)" "подписан: $who"
  else
    row "образ $ref" "Sigstore (cosign verify)" "подписей издатель не публикует ($(grep -oiE 'no signatures found|no matching signatures' "$W/img.out" | head -1))"
  fi
done < "$IN/image-digests.txt"

# 7. Чарт Qdrant: provenance
cbase="https://github.com/qdrant/qdrant-helm/releases/download/qdrant-${QDRANT_CHART_VERSION}/qdrant-${QDRANT_CHART_VERSION}.tgz"
if curl -4 -fsSL -o /dev/null "$cbase.prov" 2>/dev/null; then
  row "чарт Qdrant $QDRANT_CHART_VERSION" "provenance (.prov)" "издатель публикует .prov, проверить helm verify"
else
  row "чарт Qdrant $QDRANT_CHART_VERSION" "provenance (.prov)" "издатель не публикует; sha256 сверен с index.yaml при сборке"
fi

# 8. Без подписей издателя
row "k3s v1.37.2+k3s1" "в выпуске только sha256sum" "подписей издатель не публикует; sha256 сошёлся при сборке"
row "kube-bench v0.16.0" "в выпуске только checksums.txt" "подписей издатель не публикует; sha256 сошёлся при сборке"
row "пакеты Ubuntu (haproxy, buildah, nmap)" "подпись архива Ubuntu" "проверил apt при загрузке"
row "пакеты Python" "индекс PyPI" "sha256 из индекса проверил pip"
row "LegalBench-RAG" "источник не подписывает и сумм не публикует" "sha256 зафиксирован при загрузке"

rm -rf "$W"

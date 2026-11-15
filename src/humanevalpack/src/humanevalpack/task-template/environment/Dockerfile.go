
# --- Go: go1.18.4 + testify v1.8.0 (assert) for the tests, usable offline ---
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sha256=c9b099b68d93f5c5c8a8844a89f8db07eaa58270e3a1e01804f17f4cf8df02f5 ;; \
      arm64) sha256=35014d92b50d97da41dade965df7ebeb9a715da600206aa59ce1b2d05527421f ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/go.tar.gz "https://go.dev/dl/go1.18.4.linux-${arch}.tar.gz"; \
    echo "${sha256}  /tmp/go.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/go.tar.gz -C /usr/local --no-same-owner; \
    rm -rf /tmp/go.tar.gz /usr/local/go/test /usr/local/go/api /usr/local/go/doc \
      /usr/local/go/misc /usr/local/go/pkg/linux_${arch}_race; \
    /usr/local/go/bin/go version

# The unit tests import github.com/stretchr/testify/assert. This vendor bundle
# (testify v1.8.0, go-spew v1.1.1, go-difflib v1.0.0, yaml.v3 v3.0.1) is the one
# the reference setup unpacks. It is installed under GOPATH and Go runs in GOPATH
# mode, so `go test file.go` resolves it from any directory without network
# access. No Go module proxy is contacted, at build time or later: nothing here
# runs `go get` or `go mod`, and GOPROXY=off makes any such download fail
# instead of reaching out. cgo is disabled because the image has no C compiler;
# the programs compiled here are pure Go. The warm-up below compiles the
# standard packages a solution may import and the assert package into the build
# cache, which the sandbox account owns.
ENV PATH=/usr/local/go/bin:$PATH \
    GO111MODULE=off \
    GOPROXY=off \
    GOPATH=/opt/toolchain/gopath \
    GOCACHE=/opt/toolchain/gocache \
    CGO_ENABLED=0
RUN set -eux; \
    curl -fsSL -o /tmp/vendor.tar.gz \
      "https://github.com/THUDM/CodeGeeX/raw/07b8d7f10fe544890f9e665473998aed4e831314/codegeex/benchmark/humaneval-x/go/evaluation/vendor.tar.gz"; \
    echo "46dcd59eb54c4eb2ee36e9dbd64be55628e3e19b1a8e87c3b1f74700a81ccfd5  /tmp/vendor.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/vendor "$GOPATH/src"; \
    tar -xzf /tmp/vendor.tar.gz -C /tmp/vendor --no-same-owner; \
    cp -r /tmp/vendor/vendor/github.com /tmp/vendor/vendor/gopkg.in "$GOPATH/src/"; \
    rm -rf /tmp/vendor /tmp/vendor.tar.gz; \
    mkdir -p /tmp/warmup; cd /tmp/warmup; \
    printf '%s\n' \
      'package main' \
      '' \
      'import (' \
      '    "testing"' \
      '    "github.com/stretchr/testify/assert"' \
      ')' \
      'import (' \
      '    "bytes"' \
      '    "crypto/md5"' \
      '    "fmt"' \
      '    "math"' \
      '    "math/rand"' \
      '    "regexp"' \
      '    "sort"' \
      '    "strconv"' \
      '    "strings"' \
      '    "time"' \
      ')' \
      '' \
      'func Warmup() string {' \
      '    rand.Seed(time.Now().UnixNano())' \
      '    xs := []int{rand.Intn(3), 2, 1}' \
      '    sort.Ints(xs)' \
      '    var buf bytes.Buffer' \
      '    buf.WriteString(strings.Repeat(strconv.Itoa(int(math.Abs(-1))), 2))' \
      '    sum := fmt.Sprintf("%x", md5.Sum(buf.Bytes()))' \
      '    return regexp.MustCompile("[^0-9a-f]").ReplaceAllString(sum, "")' \
      '}' \
      '' \
      'func TestWarmup(t *testing.T) {' \
      '    assert := assert.New(t)' \
      '    assert.Equal(32, len(Warmup()))' \
      '}' > main_test.go; \
    go test -timeout=120s main_test.go; \
    cd /; rm -rf /tmp/warmup; \
    chown -R sandbox:sandbox "$GOCACHE"

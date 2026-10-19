#!/usr/bin/env bash
# Prefer verified HTTPS with an existing curl/GNU wget; fall back to Bash HTTP.
# Download to a temporary file, replace the output, then run. No Python is required.
# Set PV_CONFIG to a trusted private environment file, or supply PV_* variables.
(
    export LC_ALL=C
    umask 077
    if [[ ${PV_CONFIG:-} ]]; then
        [[ -f $PV_CONFIG && -r $PV_CONFIG ]] || { printf "[error] Cannot read PV_CONFIG.\n" >&2; exit 1; }
        source "$PV_CONFIG" || exit 1
    fi
    host=${PV_HOST:-}
    port=${PV_PORT:-80}
    https_host=${PV_HTTPS_HOST:-}
    https_port=${PV_HTTPS_PORT:-443}
    connect_timeout=${PV_CONNECT_TIMEOUT:-10}
    http_attempts=${PV_HTTP_ATTEMPTS:-3}
    ca_file=${PV_CA_FILE:-}
    resource=${PV_PATH:-}
    key=${PV_KEY:-}
    output=${PV_OUTPUT:-}
    work_dir=${PV_WORK_DIR:-}
    deadline=${PV_TIMEOUT:-300}
    worker=
    staged_output=

    fail() { printf '[error] %s\n' "$*" >&2; return 1; }

    [[ $host && $host != *[!a-zA-Z0-9.-]* ]] || { fail 'Invalid host.'; exit 1; }
    [[ $port && $port != *[!0-9]* && ${#port} -le 5 ]] || { fail 'Invalid port.'; exit 1; }
    port=$((10#$port))
    ((port >= 1 && port <= 65535)) || { fail 'Invalid port.'; exit 1; }
    [[ $resource == /* && $resource != *[[:space:][:cntrl:]]* ]] || { fail 'Invalid request path.'; exit 1; }
    [[ $key && $key != *[!a-zA-Z0-9]* ]] || { fail 'Set PV_KEY to the download key (letters and digits only).'; exit 1; }
    [[ $deadline && $deadline != *[!0-9]* && ${#deadline} -le 5 ]] || { fail 'PV_TIMEOUT must be whole seconds, 1..86400.'; exit 1; }
    deadline=$((10#$deadline))
    ((deadline >= 1 && deadline <= 86400)) || { fail 'PV_TIMEOUT must be whole seconds, 1..86400.'; exit 1; }
    [[ $https_host && $https_host != *[!a-zA-Z0-9.-]* ]] || { fail 'Invalid HTTPS host.'; exit 1; }
    [[ $https_port && $https_port != *[!0-9]* && ${#https_port} -le 5 ]] || { fail 'Invalid HTTPS port.'; exit 1; }
    https_port=$((10#$https_port))
    ((https_port >= 1 && https_port <= 65535)) || { fail 'Invalid HTTPS port.'; exit 1; }
    [[ $connect_timeout && $connect_timeout != *[!0-9]* && ${#connect_timeout} -le 5 ]] || { fail 'PV_CONNECT_TIMEOUT must be whole seconds, 1..86400.'; exit 1; }
    connect_timeout=$((10#$connect_timeout))
    ((connect_timeout >= 1 && connect_timeout <= 86400)) || { fail 'PV_CONNECT_TIMEOUT must be whole seconds, 1..86400.'; exit 1; }
    [[ $http_attempts && $http_attempts != *[!0-9]* && ${#http_attempts} -le 2 ]] || { fail 'PV_HTTP_ATTEMPTS must be a whole number, 1..10.'; exit 1; }
    http_attempts=$((10#$http_attempts))
    ((http_attempts >= 1 && http_attempts <= 10)) || { fail 'PV_HTTP_ATTEMPTS must be a whole number, 1..10.'; exit 1; }
    [[ ! $ca_file || ( $ca_file == /* && -f $ca_file && -r $ca_file ) ]] || { fail 'PV_CA_FILE must be an absolute, readable certificate file.'; exit 1; }
    [[ $work_dir == /* && -d $work_dir ]] || { fail 'PV_WORK_DIR must be an existing absolute directory.'; exit 1; }
    [[ $output ]] || { fail 'Set PV_OUTPUT to the destination path.'; exit 1; }
    cd "$work_dir" || { fail 'Cannot enter PV_WORK_DIR.'; exit 1; }
    [[ $output == /* ]] || output=./$output
    [[ ! -d $output ]] || { fail "Output path is a directory: $output"; exit 1; }
    for required in chmod mktemp mv rm; do
        command -v "$required" >/dev/null || { fail "$required is required."; exit 1; }
    done

    # Use one total download budget across HTTPS attempts and HTTP fallback.
    expires=$((SECONDS + deadline))
    stream_expires=$expires
    saw_response=0

    read_stream() {
        local left=$((stream_expires - SECONDS)) read_rc
        ((left > 0)) || return 124
        if IFS= read -r -t "$left" "$@" <&3; then return 0; else read_rc=$?; fi
        ((read_rc > 128 || SECONDS >= stream_expires)) && return 124
        return 1
    }

    read_header() {
        local read_rc=0
        line=
        read_stream -n 8192 line || read_rc=$?
        [[ ! $line ]] || saw_response=1
        if ((read_rc)); then
            http_retryable=1
            ((read_rc == 124)) && return 124
            fail 'Incomplete HTTP headers.'
            return 1
        fi
        [[ $line == *$'\r' && ${#line} -lt 8192 ]] || { fail 'Malformed or oversized HTTP header line.'; return 1; }
        line=${line%$'\r'}
        [[ $line != *$'\r'* ]] || { fail 'Malformed HTTP header line.'; return 1; }
        header_bytes=$((header_bytes + ${#line} + 2))
        ((header_bytes <= 65536)) || { fail 'HTTP headers exceed 64 KiB.'; return 1; }
    }

    receive() {
        local line version status reason field value length= header_bytes=0 read_rc
        local chunk want count received=0
        read_header || return $?
        stream_expires=$expires
        IFS=' ' read -r version status reason <<< "$line"
        [[ $version == HTTP/1.0 || $version == HTTP/1.1 ]] || { fail 'Invalid HTTP status line.'; return 1; }
        [[ $status == 200 ]] || { fail "HTTP ${status:-unknown}; download refused."; return 1; }
        while :; do
            read_header || return $?
            [[ $line ]] || break
            [[ $line == *:* && $line != [[:blank:]]* ]] || { fail 'Malformed HTTP header.'; return 1; }
            field=${line%%:*}
            value=${line#*:}
            value=${value#"${value%%[![:blank:]]*}"}
            value=${value%"${value##*[![:blank:]]}"}
            case $field in
                [Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Ll][Ee][Nn][Gg][Tt][Hh])
                    [[ ! $length && $value && $value != *[!0-9]* && ${#value} -le 18 ]] || { fail 'Invalid or duplicate Content-Length.'; return 1; }
                    length=$((10#$value))
                    ;;
                [Tt][Rr][Aa][Nn][Ss][Ff][Ee][Rr]-[Ee][Nn][Cc][Oo][Dd][Ii][Nn][Gg])
                    fail 'Transfer-Encoding is unsupported; serve an unencoded response with Content-Length.'
                    return 1
                    ;;
                [Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Ee][Nn][Cc][Oo][Dd][Ii][Nn][Gg])
                    [[ $value == [Ii][Dd][Ee][Nn][Tt][Ii][Tt][Yy] ]] || { fail 'Compressed response is unsupported.'; return 1; }
                    ;;
            esac
        done
        [[ $length ]] && ((length > 0)) || { fail 'A positive Content-Length is required to detect truncated files.'; return 1; }
        printf '[download] expecting %s bytes\n' "$length" >&2
        # Write only to our private temporary file until the download is complete.
        exec 4>|"$staged_output" || { fail 'Cannot write the temporary download file.'; return 1; }
        while ((received < length)); do
            want=$((length - received))
            ((want <= 16384)) || want=16384
            chunk=
            if read_stream -d '' -n "$want" chunk; then
                count=${#chunk}
                printf '%s' "$chunk" >&4 || { fail 'Writing the output file failed.'; return 1; }
                # A successful short read consumed a NUL delimiter; restore that byte.
                if ((count < want)); then
                    printf '\0' >&4 || { fail 'Writing the output file failed.'; return 1; }
                    count=$((count + 1))
                fi
                received=$((received + count))
            else
                read_rc=$?
                http_retryable=1
                ((read_rc == 124)) && return 124
                fail "Incomplete body: received $((received + ${#chunk})) of $length bytes."
                return 1
            fi
        done
        exec 4>&-
        printf '[download] received %s bytes\n' "$received" >&2
    }

    cleanup() {
        if [[ $worker ]]; then
            kill -KILL "$worker" 2>/dev/null || :
            wait "$worker" 2>/dev/null || :
            worker=
        fi
    }
    trap 'cleanup; if [[ $staged_output ]]; then command rm -f -- "$staged_output"; fi' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    # Keep the temporary file beside the output so replacement stays on one filesystem.
    staged_output=$(command mktemp "${output}.part.XXXXXX") || { fail 'Cannot create the temporary download file.'; exit 1; }

    # Return 2 only when HTTPS is unavailable before receiving any HTTP response.
    try_https() {
        local tool=$1 tool_path=$2 left=$((expires - SECONDS)) tool_rc=0 receive_rc=0 version read_rc trailing
        local -a tls_command
        ((left > 0)) || return 124
        if [[ $tool == curl ]]; then
            tls_command=("$tool_path" -q --silent --show-error --no-buffer --globoff --http1.0 --include
                --proxy '' --connect-timeout "$connect_timeout" --max-time "$left"
                --header "X-PV: $key" --header 'Accept-Encoding: identity'
                --url "https://$https_host:$https_port$resource")
            [[ ! $ca_file ]] || tls_command+=(--cacert "$ca_file")
        else
            version=$("$tool_path" --version 2>/dev/null)
            if [[ ! $version =~ ^GNU\ Wget\ ([0-9]+)\.([0-9]+) ]] ||
               ! ((10#${BASH_REMATCH[1]:-0} > 1 || (10#${BASH_REMATCH[1]:-0} == 1 && 10#${BASH_REMATCH[2]:-0} >= 12))); then
                printf '[skip] GNU wget 1.12 or newer is required for reliable error reporting.\n' >&2
                return 2
            fi
            tls_command=("$tool_path" --quiet --no-proxy --tries=1 --max-redirect=0
                --execute=check_certificate=on --save-headers --output-document=-
                --dns-timeout="$connect_timeout" --connect-timeout="$connect_timeout" --read-timeout="$left"
                --header="X-PV: $key" --header='Accept-Encoding: identity')
            [[ ! $ca_file ]] || tls_command+=(--ca-certificate="$ca_file")
            tls_command+=("https://$https_host:$https_port$resource")
        fi
        printf '[https] %s https://%s:%s%s\n' "$tool" "$https_host" "$https_port" "$resource" >&2
        # The fixed eval keeps the HTTP-only path parseable on Bash 3.2.
        # coproc is available in CentOS 6 Bash 4.1; exec keeps the PID killable.
        eval 'coproc TLS_PIPE { export WGETRC=/dev/null; exec "${tls_command[@]}" </dev/null; }'
        worker=$!
        saw_response=0
        stream_expires=$((SECONDS + connect_timeout))
        ((stream_expires <= expires)) || stream_expires=$expires
        if [[ ${TLS_PIPE[0]:-} ]] && exec 3<&"${TLS_PIPE[0]}"; then
            receive || receive_rc=$?
            if ((receive_rc == 0)); then
                stream_expires=$expires
                if read_stream -n 1 trailing; then
                    fail 'Unexpected data after the declared response body.'
                    receive_rc=1
                else
                    read_rc=$?
                    ((read_rc != 124)) || receive_rc=124
                fi
            fi
            exec 3<&-
        else
            receive_rc=1
        fi
        if ((receive_rc && saw_response)); then
            ((SECONDS < expires)) || receive_rc=124
            cleanup
            fail 'HTTPS response rejected or incomplete; HTTP fallback is disabled for this error.'
            return "$receive_rc"
        fi
        if ((receive_rc == 124)); then
            cleanup
            ((SECONDS < expires)) || return 124
            printf '[fallback] HTTPS did not respond within %ss.\n' "$connect_timeout" >&2
            return 2
        fi
        if wait "$worker"; then tool_rc=0; else tool_rc=$?; fi
        worker=
        if ((tool_rc == 0 && receive_rc == 0)); then return 0; fi
        if ((saw_response == 0)); then
            case "$tool:$tool_rc" in
                curl:1|curl:5|curl:6|curl:7|curl:28|curl:35|wget:4)
                    printf '[fallback] %s could not establish HTTPS (exit %s).\n' "$tool" "$tool_rc" >&2
                    return 2 ;;
            esac
        fi
        fail "$tool HTTPS failed (exit $tool_rc); certificate/response errors do not fall back to HTTP."
        return 1
    }

    download_http() {
        printf '[http] http://%s:%s%s (unencrypted)\n' "$host" "$port" "$resource" >&2
        exec 3<>/dev/tcp/"$host"/"$port" || { http_retryable=1; fail 'DNS resolution or TCP connection failed.'; return 1; }
        # Notify the parent over the control pipe so it can enforce a connect timeout.
        printf 'connected\n'
        printf 'GET %s HTTP/1.0\r\nHost: %s:%s\r\nX-PV: %s\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n' \
            "$resource" "$host" "$port" "$key" >&3 || { http_retryable=1; fail 'Could not send the HTTP request.'; return 1; }
        stream_expires=$((SECONDS + connect_timeout))
        ((stream_expires <= expires)) || stream_expires=$expires
        receive
    }

    try_http() {
        local left=$((expires - SECONDS)) connect_wait=$connect_timeout connect_expires message result read_rc
        http_retryable=0
        ((left > 0)) || return 124
        ((connect_wait <= left)) || connect_wait=$left
        connect_expires=$((SECONDS + connect_wait))
        # The child reports connection establishment, then exit status and retryability.
        exec 5< <(http_retryable=0; download_http; printf '%s %s\n' "$?" "$http_retryable")
        worker=$!
        if IFS= read -r -t "$connect_wait" message <&5; then :; else
            read_rc=$?
            cleanup
            exec 5<&-
            http_retryable=1
            if ((read_rc > 128 || SECONDS >= connect_expires)); then
                fail "HTTP TCP connection timed out (${connect_wait}s)."
                return 124
            fi
            fail 'HTTP connection worker ended before confirmation.'
            return 1
        fi
        if [[ $message == connected ]]; then
            left=$((expires - SECONDS))
            if ((left > 0)) && IFS= read -r -t "$left" message <&5; then :; else
                read_rc=$?
                cleanup
                exec 5<&-
                http_retryable=1
                if ((left <= 0 || read_rc > 128 || SECONDS >= expires)); then
                    fail "HTTP download deadline exceeded (${deadline}s)."
                    return 124
                fi
                fail 'HTTP download worker ended without a result.'
                return 1
            fi
        fi
        wait "$worker" 2>/dev/null || :
        worker=
        exec 5<&-
        case $message in
            '0 0'|'0 1'|'1 0'|'1 1'|'124 0'|'124 1')
                result=${message% *}
                http_retryable=${message#* }
                return "$result" ;;
            *) fail 'HTTP download worker returned an invalid result.'; return 1 ;;
        esac
    }

    downloaded=0
    if ((BASH_VERSINFO[0] >= 4)); then
        for tool in curl wget; do
            tool_path=$(type -P "$tool")
            [[ $tool_path ]] || continue
            if try_https "$tool" "$tool_path"; then downloaded=1; break; else result=$?; fi
            ((result == 2)) || exit "$result"
        done
    fi
    if ((downloaded == 0)); then
        printf '[fallback] Using Bash HTTP; Python/curl/wget are not required for this path.\n' >&2
        for ((attempt=1; attempt<=http_attempts; attempt++)); do
            printf '[http] attempt %s/%s\n' "$attempt" "$http_attempts" >&2
            if try_http; then downloaded=1; break; else result=$?; fi
            ((SECONDS < expires)) || { fail "Download deadline exceeded (${deadline}s)."; exit 124; }
            if ((http_retryable == 0 || attempt == http_attempts)); then
                fail "HTTP download failed (exit $result); any partial output is not executable."
                exit "$result"
            fi
            printf '[retry] HTTP transport interrupted; retrying from the beginning.\n' >&2
        done
    fi
    command chmod u+x "$staged_output" || { fail 'Download completed, but chmod failed.'; exit 1; }
    [[ ! -d $output ]] || { fail "Output path is a directory: $output"; exit 1; }
    command mv -f -- "$staged_output" "$output" || { fail "Download completed, but replacing the output failed: $output"; exit 1; }
    staged_output=
    printf '[run] Starting %s in %s\n' "$output" "$PWD"
    exec "$output"
)

#!/bin/sh
# A W4VE plugin in POSIX shell, with no SDK and no JSON library.
#
# It is here to check a claim rather than because anybody should write plugins
# this way: RFC-0003 says the protocol is the contract and not the language,
# and "a plugin in shell is concebible" is the sentence it uses to say the bar
# is low enough. An unproved claim in an RFC is a wish, so this proves it, and
# it passes the same conformance kit the Python SDK does.
#
# Reading JSON without a parser works here because the core's messages are
# written by one program, in a known shape. That is a fair thing for a plugin
# in shell to rely on and not a fair thing for an SDK to rely on.
#
# ⚠️ And the shape includes the whitespace. `json.dumps` puts a space after
# every colon, so matching on `"type":"invoke"` finds nothing at all. The
# conformance kit caught exactly that on the first run, which is the entire
# argument for having one.

say() {
    printf '%s\n' "$1"
}

log() {
    # stderr is the log by the contract. Anything on stdout is a message.
    printf '%s\n' "$1" >&2
}

# The handshake comes first and is not a reply to anything: a worker that waits
# to be greeted deadlocks, because the core is waiting for it.
say '{"type":"hello","plugin":"shellcheck","version":"1.0.0","protocol":1}'

while IFS= read -r line; do
    case "$line" in
        *'"type":"welcome"'*|*'"type": "welcome"'*)
            say '{"type":"command","name":"!!shell","help":"proof that a plugin can be a shell script","permission":"user"}'
            # Said last: it is what tells the core the plugin has finished
            # starting, as opposed to merely being alive.
            say '{"type":"ready"}'
            log "shellcheck is up"
            ;;
        *'"type":"invoke"'*|*'"type": "invoke"'*)
            # The id has to come back on the answer or nobody can match it.
            id=$(printf '%s' "$line" | sed -n 's/.*"id"[ ]*:[ ]*\([0-9]*\).*/\1/p')
            case "$line" in
                *'"command":"!!shell"'*|*'"command": "!!shell"'*)
                    say "{\"type\":\"result\",\"id\":${id:-0},\"ok\":true,\"value\":\"a shell script is a W4VE plugin\"}"
                    ;;
                *)
                    # A command it never registered is refused, not ignored: a
                    # silence here is a caller waiting for an answer for ever.
                    say "{\"type\":\"result\",\"id\":${id:-0},\"ok\":false,\"error\":\"no such command\"}"
                    ;;
            esac
            ;;
        *'"type":"shutdown"'*|*'"type": "shutdown"'*)
            log "shellcheck leaving"
            exit 0
            ;;
        *)
            # Anything else, including messages from a future version of the
            # core, is ignored rather than fatal.
            ;;
    esac
done

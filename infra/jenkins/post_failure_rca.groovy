// Drop-in shared lib for Jenkins post.failure blocks.
// On pipeline failure: POST signed webhook to jenkins-rca, get JSON RCA back,
// pretty-print it to console + set build description for at-a-glance triage.
//
// Uses raw HttpURLConnection (no plugin dep) to match the existing VictorOps
// pattern. Only Jenkins steps used: withCredentials.
//
// Prereqs in Jenkins:
//   - "Secret text" credential with ID `bbctl-webhook-secret` matching the
//     WEBHOOK_SECRET in AWS Secrets Manager.
//
// Usage in vars/<pipeline>.groovy:
//
//   post {
//     failure {
//       script { triggerRcaWebhook() }
//     }
//   }

def call() {
    return triggerRcaWebhook()
}

// Returns parsed RCA Map on success, null on failure. Always renders to
// console + sets build description.
def triggerRcaWebhook() {
    def rcaUrl = env.JENKINS_RCA_URL ?: 'https://jenkins-rca.jinka.in/rca/v1/rca/webhook'
    def service = (params?.SERVICE ?: env.SERVICE ?: env.JOB_NAME) as String
    def payload = groovy.json.JsonOutput.toJson([
        job: env.JOB_NAME,
        build: env.BUILD_NUMBER.toInteger(),
        service: service,
    ])

    withCredentials([string(credentialsId: 'bbctl-webhook-secret', variable: 'WEBHOOK_SECRET')]) {
        def sig = 'sha256=' + hmacSha256(WEBHOOK_SECRET, payload)
        def result
        try {
            result = postWebhook(rcaUrl, payload, sig)
        } catch (Exception e) {
            echo "[BB-AI] webhook transport error (non-fatal): ${e.message}"
            return null
        }
        if (result?.status == 200) {
            try {
                def rca = parseJson(result.body)
                renderRca(rca)
                return rca
            } catch (Exception e) {
                echo "[BB-AI] JSON parse error: ${e.message}"
                echo "[BB-AI] raw body: ${result.body?.take(500)}"
                return null
            }
        } else {
            echo "[BB-AI] HTTP ${result?.status}: ${result?.body?.take(300)}"
            return null
        }
    }
}

// Pure-Java HTTP POST. Returns [status: int, body: String]. Throws on
// transport error so caller logs once via echo (which can't run inside @NonCPS).
// @NonCPS keeps HttpURLConnection / streams off the persisted CPS heap.
@NonCPS
def postWebhook(String urlStr, String payload, String sig) {
    URL url = new URL(urlStr)
    HttpURLConnection conn = (HttpURLConnection) url.openConnection()
    conn.setRequestMethod('POST')
    conn.setDoOutput(true)
    conn.setRequestProperty('Content-Type', 'application/json')
    conn.setRequestProperty('X-Bbctl-Signature', sig)
    conn.setConnectTimeout(10_000)
    conn.setReadTimeout(60_000)   // LLM call: 10-30s typical

    OutputStream os = conn.getOutputStream()
    os.write(payload.getBytes('UTF-8'))
    os.close()

    int status = conn.getResponseCode()
    InputStream is = (status >= 200 && status < 400) ? conn.getInputStream() : conn.getErrorStream()
    String body = is != null ? is.getText('UTF-8') : ''
    conn.disconnect()
    return [status: status, body: body]
}

@NonCPS
def parseJson(String text) {
    return new groovy.json.JsonSlurper().parseText(text)
}

// Build a one-paragraph human-friendly summary suitable for VictorOps / Slack
// message field. Handles both shapes of suggested_fix (Map with
// Finding/Action/Verify keys; plain String for classes whose runbook uses a
// single-block format).
def buildAlertMessage(Map rca) {
    if (!rca) return ''
    def lines = []
    lines << "🤖 *BB-AI RCA* (class: ${rca.error_class ?: '?'}, stage: ${rca.failed_stage ?: '?'}, conf: ${rca.confidence ?: '?'})"
    lines << "Summary: ${rca.summary ?: '—'}"
    def fix = rca.suggested_fix
    if (fix instanceof Map) {
        def finding = fix.Finding ?: fix.finding
        def action = fix.Action ?: fix.action
        if (finding) lines << "Finding: ${finding}"
        if (action)  lines << "Action: ${(action as String).take(400)}"
    } else if (fix instanceof CharSequence) {
        // Single-block string — first ~400 chars give on-call enough to start
        // triage. Operator can hit Jenkins console for the full block.
        lines << "Fix: ${(fix as String).take(400)}"
    }
    if (rca.request_id) lines << "request_id: ${rca.request_id}"
    return lines.join('\n')
}

def renderRca(Map rca) {
    def cls = rca.error_class ?: 'unknown'
    def stage = rca.failed_stage ?: '—'
    def summary = rca.summary ?: '—'
    def reqId = rca.request_id ?: '—'
    def reportUrl = rcaReportUrl(reqId)

    // Minimal console block — the full RCA lives in the HTML report. Keep
    // the console output tight so operators don't have to scroll through a
    // wall of text; one click on the report URL gets them the full view.
    def lines = []
    lines << ''
    lines << '╔══════════════════════════════════════════════════════════════════╗'
    lines << '║               Jenkins Build RCA — Powered by BB-AI               ║'
    lines << '╚══════════════════════════════════════════════════════════════════╝'
    lines << "  class:        ${cls}"
    lines << "  failed_stage: ${stage}"
    lines << "  summary:      ${(summary as String).take(160)}"
    lines << ''
    lines << "  Full RCA report: ${reportUrl}"
    lines << "  request_id:      ${reqId}"
    lines << ''

    echo lines.join('\n')

    // Compact 2-line description for the Jenkins build list sidebar.
    // Jenkins truncates long descriptions in that view, so we keep the
    // visible text tight: line 1 = badges (class / stage), line 2 = trimmed
    // summary + click-through link to the full HTML report.
    def shortSummary = (summary as String).replaceAll(/\s+/, ' ').trim()
    if (shortSummary.length() > 110) {
        shortSummary = shortSummary.substring(0, 110).trim() + '…'
    }
    def descLines = []
    descLines << "<b>BB-AI:</b> <code>${cls}</code> · <code>${stage}</code>"
    descLines << "${escapeHtml(shortSummary)} <a href='${reportUrl}' target='_blank'><b>Open RCA →</b></a>"
    currentBuild.description = descLines.join('<br/>')
}

// Canonical URL for the HTML report served by jenkins-rca. Override the host
// at runtime via JENKINS_RCA_REPORT_BASE_URL if testing against a different ALB
// / endpoint. The route `/rca/v1/report/<uuid>` is served by FastAPI.
def rcaReportUrl(String requestId) {
    def base = env.JENKINS_RCA_REPORT_BASE_URL ?: 'https://jenkins-rca.jinka.in'
    return "${base}/rca/v1/report/${requestId}"
}

def wrap(String text, int width) {
    if (!text) return ['—']
    def out = []
    def cur = ''
    text.split(/\s+/).each { word ->
        if ((cur + ' ' + word).length() > width) {
            if (cur) out << cur
            cur = word
        } else {
            cur = cur ? "${cur} ${word}" : word
        }
    }
    if (cur) out << cur
    return out
}

@NonCPS
def escapeHtml(String s) {
    return s?.replace('&', '&amp;')?.replace('<', '&lt;')?.replace('>', '&gt;') ?: ''
}

@NonCPS
def hmacSha256(String secret, String body) {
    def mac = javax.crypto.Mac.getInstance('HmacSHA256')
    mac.init(new javax.crypto.spec.SecretKeySpec(secret.bytes, 'HmacSHA256'))
    return mac.doFinal(body.bytes).encodeHex().toString()
}

return this

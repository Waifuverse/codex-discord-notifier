"""Evidence-based user error summaries; no raw server/command payload forwarding."""
import re

ERRORS = {
    'sessionbudgetexceeded': ('Task token budget reached', 'This session reached its configured token budget; this is separate from the account usage allowance.', 'Review the task budget in Codex and increase it if appropriate, then resume. A usage reset may not resolve this limit.'),
    'badrequest': ('Codex request rejected', 'Codex reported an invalid request.', 'Review the task request, selected model, and settings in Codex before retrying.'),
    'threadrollbackfailed': ('Task rollback failed', 'Codex could not roll back the task.', 'Inspect the original task and working files before attempting another rollback; do not assume the rollback succeeded.'),
    'cyberpolicy': ('Request restricted by policy', 'Codex reported a cybersecurity policy restriction for this request.', 'Review the restriction in Codex and revise the task to a supported request. Repeated retries or switching models are not a remedy.'),
    'misalignmentpolicyviolation': ('Request stopped by policy', 'Codex reported a policy violation and stopped this request.', 'Open the task to review the restriction and revise the request. No automatic retry will be attempted by this alert.'),
    'responsetoomanyfailedattempts': ('Model response retry limit reached', 'Codex exhausted its response retry attempts.', 'Check the original task for partial work and the service connection before retrying. This is not evidence of account usage exhaustion.'),
    'activeturnnotsteerable': ('Task cannot accept changes yet', 'The active turn cannot accept same-turn steering.', 'Wait for the current review or context compaction to finish, then submit the change. Check whether your message is already queued before resending.'),
    'usagelimitexceeded': ('Codex usage limit reached', 'The account has reached its Codex usage allowance.', 'Wait for the reported reset, or review usage and available options in Codex. Then reply here to continue.'),
    'contextwindowexceeded': ('Task context limit reached', 'This task exceeded the model context window.', 'Open the task in Codex to compact its context or continue with a shorter context, then retry.'),
    'serveroverloaded': ('Selected model is at capacity', 'The selected model could not accept this turn because it is at capacity.', 'Retry later or select another available model in Codex.'),
    'ratelimitexceeded': ('Codex request rate limited', 'The service temporarily limited the request rate.', 'Wait before retrying. This does not by itself mean the account usage allowance is exhausted.'),
    'unauthorized': ('Codex sign-in required', 'The service rejected the current authentication.', 'Open Codex and check sign-in. Do not send passwords or tokens in Discord.'),
    'httpconnectionfailed': ('Codex connection failed', 'Codex could not connect to the model service.', 'Check the PC network and service availability, then retry.'),
    'responsestreamconnectionfailed': ('Model response connection lost', 'The model response stream could not stay connected.', 'Check the original task for partial work before retrying.'),
    'responsestreamdisconnected': ('Model response was disconnected', 'The model response ended before the turn completed.', 'Check the original task for partial work before retrying.'),
    'responsestreamlimitexceeded': ('Model response retry limit reached', 'Codex exhausted its response-stream retry attempts.', 'Check the original task and connection before retrying; this is not evidence of an account token limit.'),
    'internalservererror': ('Codex service error', 'The model service reported an internal error.', 'Retry later. If it persists, open the original task in Codex for diagnostics.'),
    'sandboxerror': ('Codex execution environment error', 'Codex reported an error in its execution environment.', 'Open the task in Codex and check the environment or permissions before retrying.'),
}


def summarize(payload):
    error=payload.get('error')
    error=error if isinstance(error,dict) else payload
    info=error.get('codexErrorInfo') or error.get('codex_error_info') or error.get('code') or ''
    details={}
    if isinstance(info,dict):
        key=next(iter(info),'')
        details=info.get(key) if isinstance(info.get(key),dict) else {}
        info=key
    code=re.sub(r'[^a-z]','',str(info).lower())
    message=error.get('message','')
    message=message if isinstance(message,str) else ''
    # Fallback only for explicit service wording, not vague mentions of "tokens".
    if not code or code=='other':
        lower=message.lower()
        for phrase,category in (("you've hit your usage limit",'usagelimitexceeded'),
            ('context window exceeded','contextwindowexceeded'),
            ('selected model is at capacity','serveroverloaded'),
            ('rate limit exceeded','ratelimitexceeded')):
            if phrase in lower: code=category; break
    if code not in ERRORS:
        return ('Codex encountered an error',
            'This turn failed, but Codex did not provide a recognized cause.\n\n'
            '**Next step:** Open the original task in Codex for details before retrying. '
            'No usage-limit or reset-time assumption has been made.')
    title,cause,action=ERRORS[code]
    text='**Cause:** '+cause
    http=details.get('httpStatusCode')
    if isinstance(http,int) and not isinstance(http,bool) and 100<=http<=599:
        text+='\n**HTTP status:** '+str(http)
        if http==429:
            text+=' (request throttled; this code alone does not identify an account usage limit).'
        elif http in (401,403):
            text+=' (authentication or access was rejected; review sign-in/access in Codex).'
    if code=='activeturnnotsteerable' and details.get('turnKind') in ('review','compact'):
        text+='\n**Current activity:** '+('review' if details['turnKind']=='review' else 'context compaction')+'.'
    if code=='usagelimitexceeded':
        # Extract only the service's tightly shaped date, never arbitrary text/URLs.
        reset=re.search(r'\btry again at ((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}(?:st|nd|rd|th)?, \d{4} \d{1,2}:\d{2} [AP]M)\b',message,re.I)
        if reset:
            text+='\n**Reported retry time:** '+reset[1]+' (timezone not specified by the error).'
        else:
            text+='\n**Reset time:** Not supplied in this error; check Codex usage.'
    text+='\n\n**Next step:** '+action
    return title,text

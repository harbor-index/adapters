"""Validate CaveBench questions and score control sequences.

The v3 parser accepts a complete JSON operations object within a model response.
The scorer compares the resulting control sets to the reference in event order.
"""
import hashlib,json,re
from functools import lru_cache
from pathlib import Path

def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out:raise ValueError('Duplicate JSON key: '+k)
        out[k]=v
    return out

def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=unique)
def read_jsonl(path):
    rows=[];seen=set()
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():continue
        r=json.loads(line,object_pairs_hook=unique)
        if r['id'] in seen:raise ValueError('Duplicate id: '+r['id'])
        seen.add(r['id']);rows.append(r)
    return rows
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def digest(obj):return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def save(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp');temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)

def valid_operations(ops,k,labels,candidates=None):
    if not isinstance(ops,list) or len(ops)!=k:return False
    allowed=set(labels);legal={frozenset(v) for v in candidates} if candidates else None
    for step in ops:
        if not isinstance(step,list) or not step or any(not isinstance(x,str) or not x for x in step):return False
        if len(set(step))!=len(step) or not set(step)<=allowed:return False
        if legal is not None and frozenset(step) not in legal:return False
    return True
def unwrap_single_json_fence(text):
    """Strip a single Markdown fence when it encloses the entire response."""
    if not isinstance(text,str):return text
    stripped=text.strip()
    if stripped.startswith('```'):
        matched=re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```",stripped,flags=re.DOTALL|re.IGNORECASE)
        if matched is None or '```' in matched.group(1):return text
        return matched.group(1).strip()
    return text

def _container_spans(text):
    """Yield the start, end and completeness of each outermost JSON container.

    Quoted brackets belong to their string. A mismatched or unclosed outer
    container yields one incomplete span, including any nested objects.
    """
    i=0
    while i<len(text):
        if text[i] not in '{[':i+=1;continue
        start=i;stack=[text[i]];quoted=False;escaped=False;malformed=False;i+=1
        while i<len(text) and stack:
            char=text[i]
            if quoted:
                if escaped:escaped=False
                elif char=='\\':escaped=True
                elif char=='"':quoted=False
            elif char=='"':quoted=True
            elif char in '{[':stack.append(char)
            elif char in '}]':
                if (stack[-1],char) not in (('{','}'),('[',']')):malformed=True
                stack.pop()
            i+=1
        yield start,i,not stack and not malformed


def display_label(label):
    return re.sub(r'^(PhaseA_|DeltaBA_)(0|90|180)deg$', r'\1\2°', label)


@lru_cache(maxsize=1)
def verbal_aliases():
    records = json.loads((Path(__file__).resolve().parent / "prompts/apparatus.json").read_bytes())
    return {raw: label for spec in records["scenes"].values() for raw, label in spec.get("display_labels", {}).items()}


def analyze_answer(text,k,labels,candidates=None,*,compact_labels=False):
    """Locate answer objects and return the parsed control sequence and status.

    Valid objects have a single operations field and one permitted set of
    controls per event. Repeats of the same sequence count as one answer,
    regardless of label order within an event. Conflicting sequences or any
    malformed answer object invalidate the response.
    """
    result={'operations':None,'status':'no_complete_answer_object',
            'candidate_count':0,'distinct_answer_count':0,'invalid_answer_objects':0,'spans':[]}
    if not isinstance(text,str):return result
    answers={}
    for start,end,complete in _container_spans(text):
        fragment=text[start:end]
        # Identify containers with a quoted operations key before parsing them.
        answer_like=bool(re.search(r"'operations'\s*:",fragment))
        for key_match in re.finditer(r'("(?:\\.|[^"\\])*")\s*:',fragment):
            try:answer_like=answer_like or json.loads(key_match.group(1))=='operations'
            except ValueError:pass
        if not answer_like:continue
        result['candidate_count']+=1
        item={'start':start,'end':end,'container_complete':complete,'valid':False}
        result['spans'].append(item)
        try:obj=json.loads(fragment,object_pairs_hook=unique) if complete else None
        except (TypeError,ValueError):obj=None
        if compact_labels and isinstance(obj,dict) and isinstance(obj.get('operations'),list):
            aliases={display_label(label):label for label in labels}
            aliases.update({verbal_aliases()[label]:label for label in labels if label in verbal_aliases()})
            obj['operations']=[
                [aliases.get(label,label) if isinstance(label,str) else label for label in step]
                if isinstance(step,list) else step for step in obj['operations']
            ]
        if not isinstance(obj,dict) or set(obj)!={'operations'} or not valid_operations(obj['operations'],k,labels,candidates):
            result['invalid_answer_objects']+=1;continue
        signature=json.dumps([sorted(step) for step in obj['operations']],ensure_ascii=False,separators=(',',':'))
        answers.setdefault(signature,obj['operations']);item['valid']=True
    result['distinct_answer_count']=len(answers)
    if result['invalid_answer_objects']:
        result['status']='invalid_or_incomplete_answer_object'
    elif len(answers)>1:
        result['status']='conflicting_complete_answers'
    elif len(answers)==1:
        result['operations']=next(iter(answers.values()))
        result['status']='unique_complete_answer'
    return result


def parse_answer(text,k,labels,candidates=None,*,compact_labels=False):
    return analyze_answer(text,k,labels,candidates,compact_labels=compact_labels)['operations']
def validate_question(q,files=True):
    for key in ('id','sample_id','prompt'):
        if not isinstance(q.get(key),str) or not q[key].strip():raise ValueError('Missing '+key)
    if type(q.get('k')) is not int or not 1<=q['k']<=6:raise ValueError('k must be 1..6')
    if type(q.get('scene')) is not int or not 1<=q['scene']<=20:raise ValueError('scene must be 1..20')
    if set(q)&{'operations','answer','target','ground_truth','reference_operations'}:raise ValueError('Private target in public question')
    images=q.get('images',[])
    if len(images)!=q['k']+1 or len(set(images))!=len(images):raise ValueError('Only ordered F0..Fk may be supplied')
    if files and any(not Path(x).is_file() for x in images):raise ValueError('Missing frame')
    labels=q.get('allowed_labels',[])
    if not labels or any(not isinstance(x,str) or not x for x in labels) or len(set(labels))!=len(labels):raise ValueError('Invalid labels')
    if q.get('candidates') and any(not valid_operations([c],1,labels) for c in q['candidates']):raise ValueError('Invalid candidates')
    return q

def score(answer,q,truth):
    if not valid_operations(truth,q['k'],q['allowed_labels'],q.get('candidates')):raise ValueError('Invalid reference '+q['id'])
    parsed=parse_answer(answer,q['k'],q['allowed_labels'],q.get('candidates'),compact_labels=q.get('prompt_style') in ('compact','narrative'))
    flags=[set(a)==set(b) for a,b in zip(parsed,truth)] if parsed is not None else [False]*q['k']
    return {'format_valid':parsed is not None,'operations':parsed,'step_correct':flags,'all_steps_correct':all(flags)}

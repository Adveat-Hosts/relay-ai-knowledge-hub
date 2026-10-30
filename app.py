import os, json, time, uuid, logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, date
from functools import wraps
from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///handoff.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Structured rotating logs. Never log request bodies, credentials, or API keys.
os.makedirs('logs', exist_ok=True)
class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {'timestamp': datetime.utcnow().isoformat()+'Z', 'level': record.levelname,
                   'message': record.getMessage()}
        for key in ('request_id','event','entity_id','latency_ms','outcome','error_type'):
            if hasattr(record, key): payload[key] = getattr(record, key)
        return json.dumps(payload)
handler = RotatingFileHandler('logs/app.log', maxBytes=1_000_000, backupCount=3)
handler.setFormatter(JsonFormatter()); app.logger.addHandler(handler); app.logger.setLevel(logging.INFO)

@app.before_request
def start_request():
    request.request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())
    request.started_at = time.time()

@app.after_request
def finish_request(response):
    response.headers['X-Request-ID'] = getattr(request, 'request_id', '')
    app.logger.info('request_complete', extra={'request_id':getattr(request,'request_id',''), 'event':'request_complete','outcome':str(response.status_code)})
    return response

def log_event(message, event, entity_id=None, **extra):
    app.logger.info(message, extra={'request_id':getattr(request,'request_id','system'), 'event':event, 'entity_id':entity_id or '', **extra})

class Meeting(db.Model):
    id=db.Column(db.Integer, primary_key=True); title=db.Column(db.String(180), nullable=False)
    meeting_date=db.Column(db.String(20), default=lambda: date.today().isoformat())
    participants=db.Column(db.Text, default=''); agenda=db.Column(db.Text, default=''); notes=db.Column(db.Text, default='')
    summary=db.Column(db.Text, default=''); decisions=db.Column(db.Text, default='[]'); catchup=db.Column(db.Text, default=''); knowledge_links=db.Column(db.Text, default='[]')
    created_at=db.Column(db.DateTime, default=datetime.utcnow)
    items=db.relationship('ActionItem', backref='meeting', cascade='all, delete-orphan')

class ActionItem(db.Model):
    id=db.Column(db.Integer, primary_key=True); meeting_id=db.Column(db.Integer, db.ForeignKey('meeting.id'), nullable=False)
    title=db.Column(db.String(240), nullable=False); owner=db.Column(db.String(120), default='Unassigned')
    due_date=db.Column(db.String(20), default=''); status=db.Column(db.String(30), default='Open')
    outcome=db.Column(db.Text, default=''); history=db.Column(db.Text, default='[]')

class Knowledge(db.Model):
    id=db.Column(db.Integer, primary_key=True); title=db.Column(db.String(180), nullable=False)
    content=db.Column(db.Text, nullable=False); created_at=db.Column(db.DateTime, default=datetime.utcnow)


def as_item(x):
    return {'id':x.id,'meeting_id':x.meeting_id,'title':x.title,'owner':x.owner,'due_date':x.due_date,'status':x.status,'outcome':x.outcome,'history':json.loads(x.history or '[]')}
def as_meeting(m):
    return {'id':m.id,'title':m.title,'date':m.meeting_date,'participants':m.participants,'agenda':m.agenda,'notes':m.notes,'summary':m.summary,'decisions':json.loads(m.decisions or '[]'),'catchup':m.catchup,'knowledge_links':json.loads(m.knowledge_links or '[]'),'items':[as_item(i) for i in m.items]}

def ai_json(prompt, system):
    """Use OpenAI when configured; surface a clear error and keep manual workflows available."""
    started=time.time()
    if not os.getenv('OPENAI_API_KEY'):
        log_event('AI call skipped: API key not configured','ai_call',latency_ms=0,outcome='not_configured')
        raise RuntimeError('AI is not configured. Add OPENAI_API_KEY to your .env file.')
    try:
        from openai import OpenAI
        client=OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        response=client.chat.completions.create(model=os.getenv('OPENAI_MODEL','gpt-4o-mini'),
            response_format={'type':'json_object'}, temperature=0.2,
            messages=[{'role':'system','content':system},{'role':'user','content':prompt}])
        result=json.loads(response.choices[0].message.content)
        log_event('AI call completed','ai_call',latency_ms=int((time.time()-started)*1000),outcome='success')
        return result
    except Exception as e:
        log_event('AI call failed','ai_call',latency_ms=int((time.time()-started)*1000),outcome='error',error_type=type(e).__name__)
        raise RuntimeError('AI request failed. Check your API key, model and network, then retry.') from e

@app.get('/')
def home(): return render_template('index.html')
@app.get('/api/overview')
def overview():
    meetings=Meeting.query.order_by(Meeting.id.desc()).all(); items=ActionItem.query.all()
    return jsonify({'meetings':[as_meeting(m) for m in meetings], 'stats':{'meetings':len(meetings),'open':sum(i.status!='Closed' for i in items),'overdue':sum(i.status!='Closed' and i.due_date and i.due_date<date.today().isoformat() for i in items),'closed':sum(i.status=='Closed' for i in items)}})
@app.post('/api/meetings')
def create_meeting():
    d=request.get_json() or {}; title=(d.get('title') or '').strip()
    if not title: return jsonify(error='Meeting title is required.'),400
    m=Meeting(title=title,meeting_date=d.get('date') or date.today().isoformat(),participants=d.get('participants',''),agenda=d.get('agenda',''),notes=d.get('notes',''))
    db.session.add(m); db.session.commit(); log_event('Meeting created','meeting_created',m.id)
    return jsonify(as_meeting(m)),201
@app.get('/api/meetings/<int:mid>')
def get_meeting(mid):
    m=db.session.get(Meeting,mid)
    return (jsonify(as_meeting(m)),200) if m else (jsonify(error='Meeting not found.'),404)
@app.patch('/api/items/<int:iid>')
def update_item(iid):
    i=db.session.get(ActionItem,iid)
    if not i:return jsonify(error='Action item not found.'),404
    d=request.get_json() or {}; before={'owner':i.owner,'status':i.status}
    if 'owner' in d and d['owner'].strip(): i.owner=d['owner'].strip()
    if d.get('status') in ('Open','In Progress','Blocked','Closed'): i.status=d['status']
    if 'due_date' in d:i.due_date=d['due_date']
    if 'outcome' in d:i.outcome=d['outcome'].strip()
    hist=json.loads(i.history or '[]')
    if before['owner']!=i.owner: hist.append({'at':datetime.utcnow().isoformat(),'change':'reassigned','from':before['owner'],'to':i.owner})
    if before['status']!=i.status: hist.append({'at':datetime.utcnow().isoformat(),'change':'status','from':before['status'],'to':i.status})
    i.history=json.dumps(hist); db.session.commit()
    if before['owner']!=i.owner:log_event('Action item reassigned','item_reassigned',i.id)
    if i.status=='Closed' and before['status']!='Closed':log_event('Action item closed','item_closed',i.id)
    return jsonify(as_item(i))
@app.post('/api/meetings/<int:mid>/carry-forward')
def carry_forward(mid):
    source=db.session.get(Meeting,mid)
    if not source:return jsonify(error='Meeting not found.'),404
    d=request.get_json() or {}; title=(d.get('title') or ('Follow-up: '+source.title)).strip()
    target=Meeting(title=title,meeting_date=d.get('date') or date.today().isoformat(),participants=source.participants,agenda='Carried forward from: '+source.title)
    db.session.add(target);db.session.flush();count=0
    for old in source.items:
        if old.status!='Closed':
            item=ActionItem(meeting_id=target.id,title=old.title,owner=old.owner,due_date=old.due_date,status=old.status,outcome='',history=json.dumps(json.loads(old.history or '[]')+[{'at':datetime.utcnow().isoformat(),'change':'carried_forward','from_meeting':source.id}]))
            db.session.add(item);count+=1
    db.session.commit();log_event('Open items carried forward','items_carried_forward',target.id)
    return jsonify({'meeting':as_meeting(target),'carried':count}),201
@app.post('/api/meetings/<int:mid>/ai-recap')
def ai_recap(mid):
    m=db.session.get(Meeting,mid)
    if not m:return jsonify(error='Meeting not found.'),404
    d=request.get_json() or {}; notes=d.get('notes',m.notes)
    docs=Knowledge.query.order_by(Knowledge.id.desc()).limit(15).all()
    context='\n'.join(f"DOCUMENT: {x.title}\n{x.content[:1800]}" for x in docs)
    prompt=f"MEETING: {m.title}\nPARTICIPANTS: {m.participants}\nAGENDA: {m.agenda}\nRAW NOTES: {notes}\nKNOWLEDGE BASE:\n{context}\n\nExtract only supported facts. Return JSON with keys summary (string), decisions (array of strings), action_items (array of objects with title, owner, due_date), catchup (short brief for an absent teammate), knowledge_links (array of objects with item and related_document). Use Unassigned or empty due_date when unknown."
    try: result=ai_json(prompt,'You are a careful meeting operations assistant. Do not invent owners, dates, decisions or facts. Return valid JSON only.')
    except RuntimeError as e:return jsonify(error=str(e)),503
    m.notes=notes;m.summary=result.get('summary','');m.decisions=json.dumps(result.get('decisions',[]));m.catchup=result.get('catchup','');m.knowledge_links=json.dumps(result.get('knowledge_links',[]))
    added=[]
    for a in result.get('action_items',[]):
        if a.get('title'):
            item=ActionItem(meeting_id=m.id,title=str(a['title'])[:240],owner=str(a.get('owner') or 'Unassigned')[:120],due_date=str(a.get('due_date') or '')[:20])
            db.session.add(item);added.append(item)
    db.session.commit();log_event('AI recap saved','ai_recap',m.id,outcome='success')
    return jsonify({'meeting':as_meeting(m),'knowledge_links':result.get('knowledge_links',[])})
@app.post('/api/meetings/<int:mid>/catchup')
def catchup(mid):
    m=db.session.get(Meeting,mid)
    if not m:return jsonify(error='Meeting not found.'),404
    open_items=[as_item(i) for i in m.items if i.status!='Closed']
    if not m.summary and not m.notes:return jsonify(error='Add meeting notes or generate a recap first.'),400
    try:
        r=ai_json(json.dumps({'meeting':as_meeting(m),'open_items':open_items}),'Write a concise handover brief for someone who missed this meeting. Explain context, decisions, open work, owners and deadlines. Return JSON with one key: brief.')
    except RuntimeError as e:return jsonify(error=str(e)),503
    m.catchup=r.get('brief','');db.session.commit();return jsonify({'catchup':m.catchup})
@app.get('/api/knowledge')
def knowledge_list():return jsonify([{'id':k.id,'title':k.title,'content':k.content} for k in Knowledge.query.order_by(Knowledge.id.desc()).all()])
@app.post('/api/knowledge')
def knowledge_add():
    d=request.get_json() or {}; title=(d.get('title') or '').strip(); content=(d.get('content') or '').strip()
    if not title or not content:return jsonify(error='Both title and content are required.'),400
    k=Knowledge(title=title,content=content);db.session.add(k);db.session.commit();log_event('Knowledge document added','knowledge_created',k.id);return jsonify({'id':k.id,'title':k.title,'content':k.content}),201
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):return jsonify(error='Resource not found.'),404
    return e
@app.errorhandler(500)
def server_error(e):
    app.logger.error('unhandled_error',extra={'request_id':getattr(request,'request_id',''),'event':'server_error','error_type':type(e).__name__})
    return jsonify(error='Something went wrong. Please retry.'),500

with app.app_context():
    db.create_all()
    if Meeting.query.count()==0:
        m=Meeting(title='Website launch readiness',meeting_date=date.today().isoformat(),participants='Aarav, Mira, Dev',agenda='Review launch checklist and unresolved blockers',notes='Team agreed to freeze the homepage copy. Mira will finish QA by Friday. Dev to confirm hosting costs. The analytics event naming needs review.')
        db.session.add(m);db.session.flush()
        db.session.add_all([ActionItem(meeting_id=m.id,title='Complete homepage QA',owner='Mira',due_date=date.today().isoformat()),ActionItem(meeting_id=m.id,title='Confirm hosting costs',owner='Dev',due_date=''),ActionItem(meeting_id=m.id,title='Review analytics event names',owner='Aarav',due_date='')])
        db.session.add(Knowledge(title='Launch checklist',content='Before launch: complete cross-browser QA, verify analytics events, confirm hosting and DNS, approve homepage copy, and document rollback steps.'))
        db.session.commit()

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT',5000)),debug=os.getenv('FLASK_DEBUG')=='1')

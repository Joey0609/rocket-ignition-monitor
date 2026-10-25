"""Score only completed recordings after visual annotation; never run detection."""
import csv
import gzip
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
names=['BV1cEuCzYEyX_P4','BV1WT8qzMEsM_P1','BV1sAtFzbEpo_P0','BV1rvbazSENY_P1','BV1CvWvzUEXf_P0','BV1dqkZBhEiA_P7','BV1yjkcBUEdc_P0','BV1AGDqBqEeg_P0','BV1oL5p6hEKv_P0','BV1FWLq6jExV_continuous']
frozen=json.loads((ROOT/'freeze.json').read_text())
labels=json.loads((ROOT/'labels.json').read_text())['missions']
results=[]
for name in names:
    out=ROOT/'runs'/name
    if not (out/'summary.json').exists():
        print(name,'INCOMPLETE');continue
    s=json.loads((out/'summary.json').read_text())
    assert s['complete'] and s['source_hashes']==frozen['source_hashes']
    assert (out/'decode.log').stat().st_size==0
    label=labels.get(name)
    n=0;unknown=0;pre=0;pre_unknown=0;pre_false_state=0;actions=[];active_false=False
    confirms=[e for e in s['events'] if e['action']=='confirmed']
    retracted={e['decision_id'] for e in s['events'] if e['action']=='retracted'}
    with gzip.open(out/'predictions.csv.gz','rt') as f:
        for r in csv.DictReader(f):
            assert int(r['sample'])==n,(name,n,r['sample'])
            assert abs(float(r['source_s'])-n/10)<1e-7
            if r['action']:actions.append((n,r['action']))
            unknown+=r['event'] in ('','None')
            if label and float(r['source_s'])<=label['last_no_glow_s']:
                pre+=1
                pre_unknown+=r['event'] in ('','None')
                pre_false_state+=r['event']=='1'
            n+=1
    assert n==s['counts']['images']
    assert unknown==s['counts']['unknown']
    assert actions==[(e['sample'],e['action']) for e in s['events']]
    false=[e for e in confirms if label and e['source_s']<=label['last_no_glow_s']]
    annotations=(label or {}).get('confirmation_annotations',[])
    scene_false=[e for e in confirms if any(abs(e['source_s']-a['source_s'])<1e-7 and a['classification']=='false_scene' for a in annotations)]
    all_false={e['decision_id']:e for e in false+scene_false}
    boundary=[e for e in confirms if label and label['last_no_glow_s']<e['source_s']<label['first_glow_s']]
    after=[e for e in confirms if label and e['source_s']>=label['first_glow_s'] and e['decision_id'] not in all_false]
    new_correct=[e for e in after if e['decision_id'] not in retracted]
    first=new_correct[0] if new_correct else None
    result={'name':name,'duration_s':s['duration_s'],'frames':n,'unknown_frames':unknown,'source_hashes':s['source_hashes'],'video_sha256':s['video_sha256'],'label':label,'confirmation_times_s':[e['source_s'] for e in confirms],'retraction_times_s':[e['source_s'] for e in s['events'] if e['action']=='retracted'],'false_pre_glow':len(false) if label else None,'false_alerts_retracted':sum(e['decision_id'] in retracted for e in false),'boundary_ambiguous_alerts':len(boundary),'first_post_glow_confirmation_s':first['source_s'] if first else None,'correct_after_glow_is_live':None if not first else not label.get('replay_only',False),'pre_glow_frames':pre,'pre_glow_unknown_frames':pre_unknown,'pre_glow_false_state_frames':pre_false_state,'note':'A false decision remaining latched at ignition is not a new correct confirmation.'}
    result['unretracted_false_alerts']=[{'decision_id':e['decision_id'],'confirmed_s':e['source_s'],'unretracted_through_eof_s':round(s['duration_s']-e['source_s'],3),'before_first_glow_s':round(label['first_glow_s']-e['source_s'],3)} for e in false if e['decision_id'] not in retracted]
    result['false_confirmations_total']=len(all_false) if label else None
    result['false_scene_confirmations']=[e['source_s'] for e in scene_false]
    result['false_alerts_retracted']=sum(key in retracted for key in all_false)
    if first:
        result['delay_range_s']=[round(first['source_s']-label['first_glow_s'],3),round(first['source_s']-label['last_no_glow_s'],3)]
    results.append(result)
    print(name,'FRAMES',n,'ALERTS',result['confirmation_times_s'],'FALSE',result['false_pre_glow'],'RETRACTED',result['false_alerts_retracted'],'UNKNOWN',unknown,flush=True)
report={'complete':len(results)==len(names) and all(x['label'] is not None for x in results),'frozen_commit':frozen['commit'],'results':results,'total_frames':sum(x['frames'] for x in results),'total_duration_s':sum(x['duration_s'] for x in results),'total_unknown_frames':sum(x['unknown_frames'] for x in results),'pre_glow_false_alerts':sum(x['false_pre_glow'] or 0 for x in results),'false_alerts_automatically_retracted':sum(x['false_alerts_retracted'] for x in results),'rules_changed':False,'new_layout_adaptation':False}
report['false_confirmations_total']=sum(x['false_confirmations_total'] or 0 for x in results)
(ROOT/'audited_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))

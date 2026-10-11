from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from sale_monitor.comparison_priority import refresh
from sale_monitor.http import Page
from sale_monitor.runner import Collector, task_order
from sale_monitor.scheduling import select_task
from sale_monitor.storage import Store


OLD='2026-10-01T00:00:00+00:00'
NOW='2026-10-11T00:00:00+00:00'
HOME='https://shop.tsukumo.co.jp/'
QUERY='part A'
SEARCH=HOME+'search?'+urlencode({'end_of_sales':1,'keyword':QUERY})
CFG={'stores':{'tsukumo':{'adapter':'html','seed_urls':[HOME],'product_patterns':[r'/goods/\d+'],'sale_patterns':[]}}}


class FakeClient:
    def __init__(self,pages):self.pages=pages;self.count=0
    def get(self,url):
        self.count+=1
        return Page(url,self.pages[url].encode(),NOW)


def queued(**extra):
    return {'type':'list','kind':'comparison','url':SEARCH,'created_at':OLD,'attempts':7,'priority':3,**extra}


class ComparisonPriorityTests(unittest.TestCase):
    def test_search_result_product_and_pagination_keep_active_query_and_original_age(self):
        second=SEARCH+'&page=2'
        html='<a href="/goods/1234567890123/">Part</a><a rel="next" href="'+second+'">next</a>'
        client=FakeClient({HOME:'<form action="/search"><input type="hidden" name="end_of_sales" value="1"><input name="keyword"></form>',SEARCH:html})
        with tempfile.TemporaryDirectory() as directory:
            c=Collector(Path(directory),'tsukumo',CFG,'r1',client)
            c.seed();c.state['queue'].clear()
            c.process({'type':'search','kind':'comparison','query':QUERY,'created_at':OLD,'priority':1,'requested':True})
            result=next(iter(c.state['queue'].values()))
            self.assertTrue(result['requested'])
            self.assertEqual(result['comparison_queries'],[QUERY])
            c.process(result)
            for child in c.state['queue'].values():
                self.assertTrue(child['requested'])
                self.assertEqual(child['comparison_queries'],[QUERY])
                self.assertEqual((child['created_at'],child['priority']),(OLD,1))
            self.assertEqual({t['type'] for t in c.state['queue'].values()},{'list','product'})
            self.assertEqual(c.state['offers'],{})

    def test_seed_promotes_existing_legacy_children_without_resetting_or_completing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); disk=Store(root)
            old={'page':queued(),'product':queued(type='product',url=HOME+'goods/123/',source=SEARCH),
                 'unrelated':queued(url=HOME+'search?keyword=unrelated')}
            disk.save('stores/tsukumo.json',{'store':'tsukumo','run_id':'previous','queue':old,'offers':{},'journal':[], 'done':[]})
            disk.save('requests/tsukumo.json',[{'query':QUERY,'identity':'model:brand:part','priority':1}])
            c=Collector(root,'tsukumo',CFG,'r1');c.seed()
            for key in old:
                self.assertEqual(c.state['queue'][key]['created_at'],OLD)
                self.assertEqual(c.state['queue'][key]['attempts'],7)
            self.assertTrue(c.state['queue']['page']['requested'])
            self.assertTrue(c.state['queue']['product']['requested'])
            self.assertNotIn('requested',c.state['queue']['unrelated'])
            self.assertEqual(c.state['offers'],{})
            self.assertEqual(c.state['done'],[])

    def test_new_request_plan_rechecks_descendants_after_restart(self):
        queue={'q':queued(comparison_queries=[QUERY],requested=True)}
        saved=deepcopy(queue)
        refresh(queue,[{'query':'different','priority':0}],'tsukumo')
        self.assertNotIn('requested',queue['q'])
        refresh(queue,[{'query':QUERY,'priority':1}],'tsukumo')
        self.assertTrue(queue['q']['requested'])
        self.assertEqual(queue['q']['attempts'],saved['q']['attempts'])
        self.assertEqual(queue['q']['created_at'],saved['q']['created_at'])

    def test_deduplicated_product_keeps_all_query_origins_and_oldest_attempt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            c=Collector(Path(directory),'tsukumo',CFG,'r1')
            first=queued(type='product',url=HOME+'goods/123/',source=SEARCH,comparison_queries=[QUERY])
            key=c.enqueue(first)
            c.enqueue({**first,'created_at':NOW,'attempts':0,'source':HOME+'search?keyword=second','comparison_queries':['second'],'requested':True})
            self.assertEqual(len(c.state['queue']),1)
            row=c.state['queue'][key]
            self.assertEqual(row['comparison_queries'],[QUERY,'second'])
            self.assertEqual((row['created_at'],row['attempts']),(OLD,7))
            refresh(c.state['queue'],[{'query':'second'}],'tsukumo')
            self.assertTrue(row['requested'])

    def test_query_similarity_wrong_host_and_sale_source_do_not_promote(self):
        queue={'substring':queued(url=HOME+'search?keyword=part+AB'),
               'external':queued(url='https://other.example/search?keyword=part+A'),
               'duplicate':queued(url=SEARCH+'&keyword=second'),
               'wrong_path':queued(url=HOME+'goods/123/?keyword=part+A'),
               'malformed':queued(url='https://['),
               'sale':queued(kind='sale',comparison_queries=[QUERY])}
        before=deepcopy(queue)
        refresh(queue,[{'query':QUERY}],'tsukumo')
        self.assertTrue(all(not t.get('requested') for t in queue.values()))
        self.assertEqual(queue.keys(),before.keys())

    def test_both_persisted_sofmap_query_fields_can_regain_current_priority(self):
        queue={'old':queued(url='https://www.sofmap.com/search_result.aspx',query=QUERY),
               'new':queued(url='https://www.sofmap.com/search_result.aspx',search_query=QUERY)}
        refresh(queue,[{'query':QUERY}],'sofmap')
        self.assertTrue(all(t.get('requested') for t in queue.values()))
        self.assertTrue(all(t['attempts']==7 and t['created_at']==OLD for t in queue.values()))

    def test_active_children_get_share_while_unrequested_backlog_still_runs(self):
        queue={f'active{i}':queued() for i in range(8)}
        queue.update({f'old{i}':queued(url=HOME+'search?keyword=older') for i in range(8)})
        refresh(queue,[{'query':QUERY}],'tsukumo')
        pending=list(queue.items());scheduler={};selected=[]
        for _ in range(4):
            value=select_task(pending,scheduler,task_order,datetime.fromisoformat(NOW));selected.append(value[0]);pending.remove(value)
        self.assertEqual(sum(key.startswith('active') for key in selected),3)
        self.assertEqual(sum(key.startswith('old') for key in selected),1)
        self.assertEqual(len(queue),16)

    def test_koubou_search_descendants_keep_requested_origin(self):
        from test_koubou_search import CFG as KCFG, Client, page, payload
        client=Client(lambda url:page(payload(query=QUERY,hits=21),query=QUERY))
        with tempfile.TemporaryDirectory() as directory:
            c=Collector(Path(directory),'koubou',KCFG,'r1',client)
            c.seed()
            c.process_koubou_search({'type':'search','kind':'comparison','query':QUERY,'created_at':OLD,'requested':True},QUERY)
            self.assertEqual(len(c.state['queue']),21)
            self.assertTrue(all(t.get('requested') and t['comparison_queries']==[QUERY] for t in c.state['queue'].values()))

    def test_sofmap_deferred_pages_and_products_keep_requested_origin(self):
        url='https://www.sofmap.com/search_result.aspx?keyword=part+A'
        cfg={'stores':{'sofmap':{'adapter':'html','seed_urls':[]}}}
        products=[{'url':'https://www.sofmap.com/product_detail.aspx?sku=123','kind':'comparison'}]
        dependencies=[{'url':url+'&page=2','search_query':QUERY}]
        with tempfile.TemporaryDirectory() as directory:
            c=Collector(Path(directory),'sofmap',cfg,'r1',FakeClient({url:'unused'}))
            c.seed()
            with patch('sale_monitor.runner.sofmap_search.deferred_url',return_value=None), \
                 patch('sale_monitor.runner.sofmap_search.parse_listing',return_value=(products,dependencies,{})):
                c.process_sofmap_search({'type':'list','kind':'comparison','url':url,'search_query':QUERY,'created_at':OLD,'requested':True})
            self.assertEqual(len(c.state['queue']),2)
            self.assertTrue(all(t.get('requested') and t['comparison_queries']==[QUERY] for t in c.state['queue'].values()))


if __name__=='__main__':
    unittest.main()

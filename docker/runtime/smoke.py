"""Synthetic runtime checks only. No external navigation or source publication."""
import sys,importlib
from pathlib import Path
if sys.argv[1]=='browser':
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        page=browser.new_page();page.set_content('<title>FNB fixture</title>');assert page.title()=='FNB fixture';browser.close()
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    options=webdriver.ChromeOptions();options.binary_location='/usr/bin/chromium'
    for arg in ['--headless=new','--no-sandbox','--disable-dev-shm-usage']:options.add_argument(arg)
    driver=webdriver.Chrome(service=Service('/usr/bin/chromedriver'),options=options)
    try:driver.get('data:text/html,<title>FNB fixture</title>');assert driver.title=='FNB fixture'
    finally:driver.quit()
else:
    for name in ['overture.silver','google.places_silver','google.reviews_silver','integration.entity_resolution','gold.staging','reference.area_demographics']:
        importlib.import_module('fnb_platform.'+name)
    from fnb_platform.overture.catalog import build_overture_catalog
    from shapely.geometry import Point
    ward=dict(type='Feature',properties=dict(admin_area_id='fixture',admin_area_name='Fixture',is_current=True),geometry=dict(type='Polygon',coordinates=[[[0,0],[2,0],[2,2],[0,2],[0,0]]]))
    row=dict(id='fixture',names={'primary':'Fixture'},taxonomy={'primary':'cafe','hierarchy':['food_and_drink','cafe']},geometry=Point(1,1).wkb)
    catalog=build_overture_catalog([row],ward,observed_at='2026-01-01T00:00:00Z')
    assert len(catalog.places)==1
print('RUNTIME_SMOKE_PASS',sys.argv[1])

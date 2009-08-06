#!/usr/bin/env python
# vim: ai ts=4 sts=4 et sw=4

from django.db import models
from django.utils.translation import ugettext_lazy as _
from django.db.models.signals import post_save
from django.template.loader import render_to_string
import datetime

#this is hacky until the email backend is incorporated fully
from hq.reporter.agents import *

class LogTrack(models.Model):
    """
    LogTrack is a verbose means to store any arbitrary log information 
    into a python model.
    
    Using the admin.py as the way to instantiate the handler, all log messages 
    will be stored with all relevant data into the database.
    """        
    level = models.IntegerField(null=True)
    channel = models.CharField(max_length=128, null=True)
    created = models.DateTimeField(auto_now_add=True)
    message = models.TextField(null=True)    
    pathname = models.TextField(null=True)
    funcname = models.CharField(max_length=128,null=True)
    module = models.CharField(max_length=128,null=True)
    filename = models.CharField(max_length=128, null=True)
    line_no = models.IntegerField(null=True)    
    traceback = models.TextField(null=True)
    
    #If we can enable kwargs for debug message emits, then this would be useful.
    #but a good workaround is to WRITE DETAILED MESSAGES
    #extras = models.TextField(null=True)
    
    def __unicode__(self):
        return self.message
    class Meta:
        verbose_name = _("Log Tracker")


# Signal registration is done here in the models instead of the views because
# everytime someone hits a view and that messes up the process registration
# whereas models is loaded once
def sendAlert(sender, instance, *args, **kwargs): #get sender, instance, created    
    eml = EmailAgent()    
    context = {}
    context['log'] = instance    
    rendered_text = render_to_string("logtracker/alert_display.html", context)
    
    #Send it to an email address baked into the settings/ini file.
    eml.send_email("[Commcare-hq Alert] " + instance.message, 
                   settings.RAPIDSMS_APPS['logtracker']['default_alert_email'], 
                   rendered_text)    
                
    
# Register to receive signals from LogTrack
post_save.connect(sendAlert, sender=LogTrack)

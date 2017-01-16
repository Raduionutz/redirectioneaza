# -*- coding: utf-8 -*-

from urlparse import urlparse

from google.appengine.api import urlfetch
from google.appengine.ext import ndb

from hashlib import sha1
from webapp2_extras import json, security

from appengine_config import SECRET_KEY, AWS_PDF_URL, LIST_OF_COUNTIES, USER_UPLOADS_FOLDER, USER_FORMS
# also import captcha settings
from appengine_config import CAPTCHA_PRIVATE_KEY, CAPTCHA_POST_PARAM

from models.handlers import BaseHandler
from models.models import NgoEntity, Donor
from models.storage import CloudStorage
from models.create_pdf import create_pdf


from captcha import submit

from logging import info
import re
import json
import datetime

ngo = NgoEntity(
    logo= "http://images.clipartpanda.com/spinner-clipart-9cz75npcE.jpeg",
    name= "Nume asoc",
    description= "o descriere lunga",
    id= "ssda",
    account = "RO33BTRL3342234vvf2234234234XX",
    cif = "3333223",
    address = "Str. Ion Ionescu, nr 33"
)
# ngo.put()

"""
Handlers used for ngo 
"""
class NgoHandler(BaseHandler):
    def get(self, ngo_url):

        self.redirect( self.uri_for("twopercent", ngo_url=ngo_url) )


class TwoPercentHandler(BaseHandler):
    template_name = 'twopercent.html'

    def get(self, ngo_url):

        ngo = NgoEntity.get_by_id(ngo_url)
        # if we didn't find it or the ngo doesn't have an active page
        if ngo is None or ngo.active == False:
            self.error(404)
            return

        # if we still have a cookie from an old session, remove it
        if "donor_id" in self.session:
            self.session.pop("donor_id")

        if "has_cnp" in self.session:
            self.session.pop("has_cnp")
            # also we can use self.session.clear(), but it might delete the logged in user's session
        
        self.template_values["title"] = "Donatie 2%"
        self.template_values["ngo"] = ngo
        self.template_values["counties"] = LIST_OF_COUNTIES
        
        # the ngo website
        ngo_website = ngo.website if ngo.website else None
        if ngo_website:
            # try and parse the the url to see if it's valid
            try:
                url_dict = urlparse(ngo_website)


                if not url_dict.scheme:
                    url_dict = url_dict._replace(scheme='http')


                # if we have a netloc, than the url is valid
                # use the netloc as the website name
                if url_dict.netloc:
                
                    self.template_values["ngo_website_description"] = url_dict.netloc
                    self.template_values["ngo_website"] = url_dict.geturl()
                
                # of we don't have the netloc, when parsing the url
                # urlparse might send it to path
                # move that to netloc and remove the path
                elif url_dict.path:
                    
                    url_dict = url_dict._replace(netloc=url_dict.path)
                    self.template_values["ngo_website_description"] = url_dict.path
                    
                    url_dict = url_dict._replace(path='')
                
                    self.template_values["ngo_website"] = url_dict.geturl()
                else:
                    raise

            except Exception, e:

                self.template_values["ngo_website"] = None
        else:

            self.template_values["ngo_website"] = None    


        now = datetime.datetime.now()
        can_donate = True
        if now.month > 5 or now.month == 5 and now.day > 25:
            can_donate = False

        self.template_values["can_donate"] = can_donate
        
        self.render()

    def post(self, ngo_url):

        post = self.request
        errors = {
            "fields": [],
            "server": False
        }

        self.ngo = NgoEntity.get_by_id(ngo_url)
        if self.ngo is None:
            self.error(404)
            return

        # if we have an ajax request, just return an answer
        self.is_ajax = self.request.get("ajax", False)

        def get_post_value(arg, add_to_error_list=True):
            value = post.get(arg)

            # if we received a value, it should only contains alpha numeric, spaces and dash
            if value:
                if re.match('^[\w\s.-]+$', value) is not None:
                    # additional validation
                    if arg == "cnp" and len(value) != 13:
                        errors["fields"].append(arg)
                        return ""

                    return value
                else:
                    errors["fields"].append(arg)
            
            elif add_to_error_list:
                errors["fields"].append(arg)

            return ""

        payload = {}

        # the donor's data
        payload["first_name"] = get_post_value("nume").title()
        payload["last_name"] = get_post_value("prenume").title()
        payload["father"] = get_post_value("tatal").title()
        payload["cnp"] = get_post_value("cnp", False)

        payload["street"] = get_post_value("strada").title()
        payload["number"] = get_post_value("numar")

        # optional data
        payload["bl"] = get_post_value("bloc", False)
        payload["sc"] = get_post_value("scara", False)
        payload["et"] = get_post_value("etaj", False)
        payload["ap"] = get_post_value("ap", False)

        payload["city"] = get_post_value("localitate").title()
        payload["county"] = get_post_value("judet")

        # the ngo data
        ngo_data = {
            "name": self.ngo.name,
            "account": self.ngo.account,
            "cif": self.ngo.cif
        }

        # payload["secret_key"] = SECRET_KEY
        
        if len(errors["fields"]):
            self.return_error(errors)
            return

        captcha_response = submit(post.get(CAPTCHA_POST_PARAM), CAPTCHA_PRIVATE_KEY, self.request.remote_addr)

        # if the captcha is not valid return
        if not captcha_response.is_valid:
            
            errors["fields"].append("codul captcha")
            self.return_error(errors)
            return

        # the user's folder name, it's just his md5 hashed db id
        user_folder = security.hash_password('123', "md5")

        # a way to create unique file names
        # get the local time in iso format
        # run that through SHA1 hash
        # output a hex string
        filename = "{0}/{1}/{2}".format(USER_FORMS, str(user_folder), sha1( datetime.datetime.now().isoformat() ).hexdigest())

        pdf = create_pdf(payload, ngo_data)

        file_url = CloudStorage.save_file(pdf, filename)

        # close the file after it has been uploaded
        pdf.close()

        # prepare the donor entity while we wait for aws
        donor = Donor(
            first_name = payload["first_name"],
            last_name = payload["last_name"],
            city = payload["city"],
            county = payload["county"],
            # make a request to get geo ip data for this user
            geoip = self.get_geoip_data(),
            ngo = self.ngo.key,
            pdf_url = file_url
        )

        # only save if the pdf was created
        donor.put()

        # set the donor id in cookie
        self.session["donor_id"] = str(donor.key.id())
        self.session["has_cnp"] = bool(payload["cnp"])

        # if not an ajax request, redirect
        if self.is_ajax:
            self.response.set_status(200)
            self.response.write(json.dumps({}))
        else:
            self.redirect( self.uri_for("twopercent-step-2", ngo_url=ngo_url) )

    def return_error(self, errors):
        
        if self.is_ajax:

            self.response.set_status(400)
            self.response.write(json.dumps(errors))

            return

        self.template_values["title"] = "Donatie 2%"
        self.template_values["ngo"] = self.ngo
        
        self.template_values["counties"] = LIST_OF_COUNTIES
        self.template_values["errors"] = errors
        
        for key in self.request.POST:
            self.template_values[ key ] = self.request.POST[ key ]

        # render a response
        self.render()

class TwoPercent2Handler(BaseHandler):
    template_name = 'twopercent-2.html'

    errors = {
        "missing_values": "Te rugam sa completezi cu o adresa de email sau un numar de telefon.",
        "invalid_email": "Te rugam sa introduci o adresa de email valida.",
        "invalid_tel": "Te rugam sa introduci un numar de telefon mobil valid."
    }

    def get(self, ngo_url):

        if self.get_ngo_and_donor() is False:
            return

        # set the index template
        self.template_values["title"] = "Donatie 2%"
        self.template_values["ngo"] = self.ngo
        
        # render a response
        self.render()

    def post(self, ngo_url):
        post = self.request
        # bool that tells us if its a ajax request
        # we don't need to set any template if this is the case
        is_ajax = post.get("ajax", False)
        error_message = ""

        if self.get_ngo_and_donor() is False:
            self.abort(404)

        # strip any white space
        email = post.get("email").strip() if post.get("email") else ""
        # also remove any dots that might be in the phone number
        tel = post.get("tel").strip().replace(".", "") if post.get("tel") else ""

        # if we have no email or tel
        if not email and not tel:
            error_message = self.errors["missing_values"]
        else:
            # else validate email
            email_re = re.compile('[\w.-]+@[\w.-]+.\w+')
            if email and not email_re.match(email):
                error_message = self.errors["invalid_email"]

            # or validate tel
            if tel and len(tel) != 10 and tel[:2] != "07":
                error_message = self.errors["invalid_tel"]
        
        info(error_message)

        # if it's not an ajax request
        # and we have some error_message
        if len(error_message) != 0:

            if is_ajax:
                self.response.set_status(400)
                error = {
                    "message": error_message
                }
                self.response.write(json.dumps(error))
            else:
                
                self.template_values["ngo"] = self.ngo
                self.template_values["error_message"] = error_message
                self.template_values["email"] = email
                self.template_values["tel"] = tel
                
                self.render()

        else:
            self.donor.email = email
            self.donor.tel = tel

            self.donor.put()

            # send and email to the donor with a link to the PDF file
            self.send_email("twopercent-form", self.donor)

            # if ajax return 200 and the url
            if is_ajax:
                self.response.set_status(200)
                response = {
                    "url": self.uri_for("ngo-twopercent-success", ngo_url=ngo_url)
                }
                self.response.write(json.dumps(response))
            else:
                # if not, redirect to succes
                self.redirect( self.uri_for("ngo-twopercent-success", ngo_url=ngo_url) )


class DonationSucces(BaseHandler):
    template_name = 'succes.html'
    def get(self, ngo_url):

        if self.get_ngo_and_donor() is False:
            return

        self.template_values["ngo"] = self.ngo
        self.template_values["donor"] = self.donor
        self.template_values["title"] = "Donatie 2% - succes"

        info(self.session.get("has_cnp"))

        # if the user didn't provide a CNP show a message
        self.template_values["has_cnp"] = self.session.get("has_cnp", False)


        self.render()

    def post(self, ngo_url):
        # TODO: to be implemented
        post = self.request

        if self.get_ngo_and_donor() is False:
            return

        self.session.pop("donor_id")

        signed_pdf = post.get("signed-pdf")

        # TODO file upload
        
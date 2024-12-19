import json
import logging
import time
import urllib
import uuid
from typing import Dict, List, Generator, Iterator
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
from PIL import Image, ImageEnhance, ImageOps
import pytesseract
from io import BytesIO
import requests
from requests_html import HTMLSession
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from utils import (
    generate_offline_threading_id,
    extract_value,
    format_response,
)
from utils import get_fb_session, get_session
from exceptions import FacebookRegionBlocked

app = FastAPI()
# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this to specify allowed origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)
pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

MAX_RETRIES = 3
THREAD_ID="ebe0e45e-27d2-459b-a3d2-792ed1a71a56"
# THREAD_ID="fc0099a3-544a-4a0f-b300-5d2e3823dd67"
COOKIE_TOKEN="Fqz98dCNnsEBFlQYDmoxNXp2SVJndWpfbXpnFpKg%2BvMMAA%3D%3D"

class MetaAI:
    """
    A class to interact with the Meta AI API to obtain and use access tokens for sending
    and receiving messages from the Meta AI Chat API.
    """

    def __init__(
        self, fb_email: str = None, fb_password: str = None, proxy: dict = None
    ):
        self.session = get_session()
        self.session.headers.update(
            {
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            }
        )
        self.access_token = None
        self.fb_email = fb_email
        self.fb_password = fb_password
        self.proxy = proxy

        self.is_authed = fb_password is not None and fb_email is not None
        self.cookies = self.get_cookies()
        self.external_conversation_id = THREAD_ID
        self.offline_threading_id = None

    def get_access_token(self) -> str:
        """
        Retrieves an access token using Meta's authentication API.

        Returns:
            str: A valid access token.
        """

        if self.access_token:
            return self.access_token

        url = "https://www.meta.ai/api/graphql/"
        payload = {
            "lsd": self.cookies["lsd"],
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "useAbraAcceptTOSForTempUserMutation",
            "variables": {
                "dob": "1999-01-01",
                "icebreaker_type": "TEXT",
                "__relay_internal__pv__WebPixelRatiorelayprovider": 1,
            },
            "doc_id": "7604648749596940",
        }
        payload = urllib.parse.urlencode(payload)  # noqa
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "cookie": f'_js_datr={self.cookies["_js_datr"]}; '
            f'abra_csrf={self.cookies["abra_csrf"]}; datr={self.cookies["datr"]};',
            "sec-fetch-site": "same-origin",
            "x-fb-friendly-name": "useAbraAcceptTOSForTempUserMutation",
        }

        response = self.session.post(url, headers=headers, data=payload)

        try:
            auth_json = response.json()
        except json.JSONDecodeError:
            raise FacebookRegionBlocked(
                "Unable to receive a valid response from Meta AI. This is likely due to your region being blocked. "
                "Try manually accessing https://www.meta.ai/ to confirm."
            )

        access_token = auth_json["data"]["xab_abra_accept_terms_of_service"][
            "new_temp_user_auth"
        ]["access_token"]

        # Need to sleep for a bit, for some reason the API doesn't like it when we send request too quickly
        # (maybe Meta needs to register Cookies on their side?)
        time.sleep(1)

        return access_token

    def prompt(
        self,
        message: str,
        stream: bool = False,
        attempts: int = 0,
        new_conversation: bool = False,
    ) -> Dict or Generator[Dict, None, None]:
        """
        Sends a message to the Meta AI and returns the response.

        Args:
            message (str): The message to send.
            stream (bool): Whether to stream the response or not. Defaults to False.
            attempts (int): The number of attempts to retry if an error occurs. Defaults to 0.
            new_conversation (bool): Whether to start a new conversation or not. Defaults to False.

        Returns:
            dict: A dictionary containing the response message and sources.

        Raises:
            Exception: If unable to obtain a valid response after several attempts.
        """
        if not self.is_authed:
            self.access_token = self.get_access_token()
            auth_payload = {"access_token": self.access_token}
            url = "https://graph.meta.ai/graphql?locale=user"

        else:
            auth_payload = {"fb_dtsg": self.cookies["fb_dtsg"]}
            url = "https://www.meta.ai/api/graphql/"

        if not self.external_conversation_id or new_conversation:
            external_id = str(uuid.uuid4())
            self.external_conversation_id = external_id
        payload = {
            **auth_payload,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "useAbraSendMessageMutation",
            "variables": json.dumps(
                {
                    "message": {"sensitive_string_value": message},
                    "externalConversationId": self.external_conversation_id,
                    "offlineThreadingId": generate_offline_threading_id(),
                    "suggestedPromptIndex": None,
                    "flashVideoRecapInput": {"images": []},
                    "flashPreviewInput": None,
                    "promptPrefix": None,
                    "entrypoint": "ABRA__CHAT__TEXT",
                    "icebreaker_type": "TEXT",
                    "__relay_internal__pv__AbraDebugDevOnlyrelayprovider": False,
                    "__relay_internal__pv__WebPixelRatiorelayprovider": 1,
                }
            ),
            "server_timestamps": "true",
            "doc_id": "7783822248314888",
        }
        payload = urllib.parse.urlencode(payload)  # noqa
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "x-fb-friendly-name": "useAbraSendMessageMutation",
        }
        if self.is_authed:
            headers["cookie"] = f'abra_sess={self.cookies["abra_sess"]}'
            # Recreate the session to avoid cookie leakage when user is authenticated
            self.session = requests.Session()
            self.session.proxies = self.proxy

        response = self.session.post(url, headers=headers, data=payload, stream=stream)
        if not stream:
            raw_response = response.text
            last_streamed_response = self.extract_last_response(raw_response)
            if not last_streamed_response:
                return self.retry(message, stream=stream, attempts=attempts)

            extracted_data = self.extract_data(last_streamed_response)
            return extracted_data

        else:
            lines = response.iter_lines()
            is_error = json.loads(next(lines))
            if len(is_error.get("errors", [])) > 0:
                return self.retry(message, stream=stream, attempts=attempts)
            return self.stream_response(lines)

    def retry(self, message: str, stream: bool = False, attempts: int = 0):
        """
        Retries the prompt function if an error occurs.
        """
        if attempts <= MAX_RETRIES:
            logging.warning(
                f"Was unable to obtain a valid response from Meta AI. Retrying... Attempt {attempts + 1}/{MAX_RETRIES}."
            )
            time.sleep(3)
            return self.prompt(message, stream=stream, attempts=attempts + 1)
        else:
            raise Exception(
                "Unable to obtain a valid response from Meta AI. Try again later."
            )

    def extract_last_response(self, response: str) -> Dict:
        """
        Extracts the last response from the Meta AI API.

        Args:
            response (str): The response to extract the last response from.

        Returns:
            dict: A dictionary containing the last response.
        """
        last_streamed_response = None
        for line in response.split("\n"):
            try:
                json_line = json.loads(line)
            except json.JSONDecodeError:
                continue

            bot_response_message = (
                json_line.get("data", {})
                .get("node", {})
                .get("bot_response_message", {})
            )
            chat_id = bot_response_message.get("id")
            if chat_id:
                external_conversation_id, offline_threading_id, _ = chat_id.split("_")
                self.external_conversation_id = external_conversation_id
                self.offline_threading_id = offline_threading_id

            streaming_state = bot_response_message.get("streaming_state")
            if streaming_state == "OVERALL_DONE":
                last_streamed_response = json_line

        return last_streamed_response

    def stream_response(self, lines: Iterator[str]):
        """
        Streams the response from the Meta AI API.

        Args:
            lines (Iterator[str]): The lines to stream.

        Yields:
            dict: A dictionary containing the response message and sources.
        """
        for line in lines:
            if line:
                json_line = json.loads(line)
                extracted_data = self.extract_data(json_line)
                if not extracted_data.get("message"):
                    continue
                yield extracted_data

    def extract_data(self, json_line: dict):
        """
        Extract data and sources from a parsed JSON line.

        Args:
            json_line (dict): Parsed JSON line.

        Returns:
            Tuple (str, list): Response message and list of sources.
        """
        bot_response_message = (
            json_line.get("data", {}).get("node", {}).get("bot_response_message", {})
        )
        response = format_response(response=json_line)
        fetch_id = bot_response_message.get("fetch_id")
        sources = self.fetch_sources(fetch_id) if fetch_id else []
        medias = self.extract_media(bot_response_message)
        return {"message": response, "sources": sources, "media": medias}

    @staticmethod
    def extract_media(json_line: dict) -> List[Dict]:
        """
        Extract media from a parsed JSON line.

        Args:
            json_line (dict): Parsed JSON line.

        Returns:
            list: A list of dictionaries containing the extracted media.
        """
        medias = []
        imagine_card = json_line.get("imagine_card", {})
        session = imagine_card.get("session", {}) if imagine_card else {}
        media_sets = (
            (json_line.get("imagine_card", {}).get("session", {}).get("media_sets", []))
            if imagine_card and session
            else []
        )
        for media_set in media_sets:
            imagine_media = media_set.get("imagine_media", [])
            for media in imagine_media:
                medias.append(
                    {
                        "url": media.get("uri"),
                        "type": media.get("media_type"),
                        "prompt": media.get("prompt"),
                    }
                )
        return medias

    def get_cookies(self) -> dict:
        """
        Extracts necessary cookies from the Meta AI main page.

        Returns:
            dict: A dictionary containing essential cookies.
        """
        session = HTMLSession()
        # print(session,'session')
        headers = {}
        if self.fb_email is not None and self.fb_password is not None:
            
            # fb_session = get_fb_session(self.fb_email, self.fb_password)
            fb_session = {'datr': 'd2tAZx_2FrJ4kCj-dvKrE74X', 'fr': '0x3X6cTMlkBg9m4mT..BnQGt3..AAA.0.0.BnQGt3.AWXVCLdQb6I', 'ps_l': '1', 'ps_n': '1', 'sb': 'dWtAZyJtmIq5q14tmm7q-S51', 'abra_sess': COOKIE_TOKEN}
            headers = {"cookie": f"abra_sess={fb_session['abra_sess']}"}
        response = session.get(
            "https://www.meta.ai/",
            headers=headers,
        )
        cookies = {
            "_js_datr": extract_value(
                response.text, start_str='_js_datr":{"value":"', end_str='",'
            ),
            "datr": extract_value(
                response.text, start_str='datr":{"value":"', end_str='",'
            ),
            "lsd": extract_value(
                response.text, start_str='"LSD",[],{"token":"', end_str='"}'
            ),
            "fb_dtsg": extract_value(
                response.text, start_str='DTSGInitData",[],{"token":"', end_str='"'
            ),
        }

        if len(headers) > 0:
            cookies["abra_sess"] = fb_session["abra_sess"]
        else:
            cookies["abra_csrf"] = extract_value(
                response.text, start_str='abra_csrf":{"value":"', end_str='",'
            )
        return cookies

    def fetch_sources(self, fetch_id: str) -> List[Dict]:
        """
        Fetches sources from the Meta AI API based on the given query.

        Args:
            fetch_id (str): The fetch ID to use for the query.

        Returns:
            list: A list of dictionaries containing the fetched sources.
        """

        url = "https://graph.meta.ai/graphql?locale=user"
        payload = {
            "access_token": self.access_token,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "AbraSearchPluginDialogQuery",
            "variables": json.dumps({"abraMessageFetchID": fetch_id}),
            "server_timestamps": "true",
            "doc_id": "6946734308765963",
        }

        payload = urllib.parse.urlencode(payload)  # noqa

        headers = {
            "authority": "graph.meta.ai",
            "accept-language": "en-US,en;q=0.9,fr-FR;q=0.8,fr;q=0.7",
            "content-type": "application/x-www-form-urlencoded",
            "cookie": f'dpr=2; abra_csrf={self.cookies.get("abra_csrf")}; datr={self.cookies.get("datr")}; ps_n=1; ps_l=1',
            "x-fb-friendly-name": "AbraSearchPluginDialogQuery",
        }

        response = self.session.post(url, headers=headers, data=payload)
        response_json = response.json()
        message = response_json.get("data", {}).get("message", {})
        search_results = (
            (response_json.get("data", {}).get("message", {}).get("searchResults"))
            if message
            else None
        )
        if search_results is None:
            return []

        references = search_results["references"]
        return references


def preprocess_image(image):
    # Convert to grayscale
    image = ImageOps.grayscale(image)
    
    # Increase contrast
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2)  # Adjust the level as needed

    # Resize image
    image = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)

    # Binarize (Thresholding)
    image = image.point(lambda x: 0 if x < 128 else 255, '1')

    return image

class ImageRequest(BaseModel):
    image: str

@app.get("/test")
async def testingapi(request: ImageRequest):
    print("testing the api")
    return "api"
    

@app.post("/api/retrieve-text")
async def retrieve_text(request: ImageRequest):
    image_url = request.image
    
    try:
        response = requests.get(image_url)
        response.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to retrieve image {e}")
    
    
    try:
        image = Image.open(BytesIO(response.content))
        processed_image = preprocess_image(image)
        config = "--oem 3"
        extracted_text = pytesseract.image_to_string(processed_image, config=config, lang="eng")
        return {"result": extracted_text}
        myprompt =  """  
        
                Convert it to json and and rephrase the product name to actual product name fix the product spellings and also give me a brand and manufactured of each product and
                give me just json not any other text
                example json structure below
                {
                "invoice_number": "163181DF2M59265259",
                "store": "KH1 - MEGA - ZAMZAMA",
                "ntn": "B353738",
                "transaction_number": "235010133704",
                "transaction_date": "Jun 2, 2024 1:59 PM",
                "user": "61895-M Ahmed",
                "pos": "KZMZ-SAL-POS-01-KZMZ-SAL-POS-01",
                "items": [
                {
                "product_desc": "Cat Tisu Emotions 100x2ply Tissues",
                "unit_price": "295.00",
                "brand": "Cat Tisu",
                "manufacturer":"",
                "measurement_units": "pieces",
                "price_per_unit": 995,
                "quantity": 1,
                "discount": 370,
                "total_price": 625
                }
                ],
                "total_items": 1,
                "total_quantity": 1,
                "discount":0,
                "invoice_value": 625,
                "gst": 100,
                "payments": {
                "method": "Keenu",
                "amount": 625,
                "card":"4659*********"
                },
                "change_due": "0.00",
                "return_policy_url": "www.imtiaz.com.pk/return-policies"
                }
                """
        # beforePrompt = """
        #         You are a bot for api to extract the data from pictures like OCR you have to give me the product details json array of objects with measurement units dont include price unit
        #             [{
        #                 'store': store,
        #                 'date': date,
        #                 'brand': brand,
        #                 'product_desc': product_desc,
        #                 'measurement_units': measurement_units,
        #                 'price_per_unit': price_per_unit,
        #                 'quantity': quantity,
        #                 'discount': discount,
        #                 'total_price': total_price,
        #             }]
        #             in this structure for each and every product in the given text 
        #         """
        # extracted_text=beforePrompt+extracted_text+myprompt
        extracted_text=extracted_text+myprompt
        ai = MetaAI(fb_email="Email", fb_password="Password")
        resp = ai.prompt(message=extracted_text, stream=False)
        message = resp['message']
        # message = extracted_text
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process image {e}")
    
    
    return {"result": message}

if __name__ == "__main__":
    
    # resp = ai.prompt(message="ae\n\nFBR Invoice #: 16318 1DF2M59265259\n\nKHL - MEGA - ZAMZAMA-\n\nNTN # B353738\n\nTransaction No.: 235010133704\nTransaction Date; Jun 2, 2024 1:59 PM\nUser: 61895-M Ahmed\n\npus KZMZ -SAL- PUS- Ol “KEME- -SAL-POS~ “OL\n\norigina a\n\nmee ae\n\nProduct Descript ion\n\n“Pakola Water 1500m)\n\n600 69,00 0.00  Rs414.00\ncat Tisu Emotions 100X2P ly\n1.00 995.00 0,00 R625 .00\n\nAxe Body Spray Musk 150m) -\n\n3.00 749.00 0.00 Rs2, 247.00\nClypso Bath Belt\n\n1.00 395.00 0.00 RS 395.00\nHankies Tisu Garden 150X2P ly\n\n3,00 249,00 0,00 RS 74700\nPerfect Air Frshnr Rfi1 Polo aul\n\n1,02 434 0 0.00 Rs439 .00\ntux BdyWet Uewy Sakura 500m]\n\n1.00 1195.00 0.00 Rs1, 155.06\nLndn Shn Shoe Shn Spng Nutr]\n\n1.00 335 010 0.00 Rs335.\n\nSor Storm Air Frehrir Rfil Lavendor >a0M\n1,00 675 0U (iti Rs525 00\nFBR POS Charges ™\n\n1.00 {ia G,00 Rs} .00\ntotal ‘Thems/tiuant ity 10/19.06\nDiscount Rs0 .00\nRounding Rs0 G0\n\nToice Vahe Fah BLD\n\nsale Tax Breakup\n\nExt. batt gs In}, Amt\nMRP Rs? 693.11 Rs522.89 Rs3 3,216.00\nNON MRP -Rs2, 755.38 Rs621. 62 Rs3,377.00\n\nPayments\n\nme we ce ET ne em wee mm\n\nKeen R56 , 593.00\nvhange Due Rs0. 00\n\n| MUL\n\nfor return 4 exchange policy details,\nVisit: www. imtiaz.com. ok/return-policies\n", stream=False)
    # # meta = MetaAI()
    # # resp = meta.prompt("How are you?", stream=False)
    # # resp = meta.prompt("ae\n\nFBR Invoice #: 16318 1DF2M59265259\n\nKHL - MEGA - ZAMZAMA-\n\nNTN # B353738\n\nTransaction No.: 235010133704\nTransaction Date; Jun 2, 2024 1:59 PM\nUser: 61895-M Ahmed\n\npus KZMZ -SAL- PUS- Ol “KEME- -SAL-POS~ “OL\n\norigina a\n\nmee ae\n\nProduct Descript ion\n\n“Pakola Water 1500m)\n\n600 69,00 0.00  Rs414.00\ncat Tisu Emotions 100X2P ly\n1.00 995.00 0,00 R625 .00\n\nAxe Body Spray Musk 150m) -\n\n3.00 749.00 0.00 Rs2, 247.00\nClypso Bath Belt\n\n1.00 395.00 0.00 RS 395.00\nHankies Tisu Garden 150X2P ly\n\n3,00 249,00 0,00 RS 74700\nPerfect Air Frshnr Rfi1 Polo aul\n\n1,02 434 0 0.00 Rs439 .00\ntux BdyWet Uewy Sakura 500m]\n\n1.00 1195.00 0.00 Rs1, 155.06\nLndn Shn Shoe Shn Spng Nutr]\n\n1.00 335 010 0.00 Rs335.\n\nSor Storm Air Frehrir Rfil Lavendor >a0M\n1,00 675 0U (iti Rs525 00\nFBR POS Charges ™\n\n1.00 {ia G,00 Rs} .00\ntotal ‘Thems/tiuant ity 10/19.06\nDiscount Rs0 .00\nRounding Rs0 G0\n\nToice Vahe Fah BLD\n\nsale Tax Breakup\n\nExt. batt gs In}, Amt\nMRP Rs? 693.11 Rs522.89 Rs3 3,216.00\nNON MRP -Rs2, 755.38 Rs621. 62 Rs3,377.00\n\nPayments\n\nme we ce ET ne em wee mm\n\nKeen R56 , 593.00\nvhange Due Rs0. 00\n\n| MUL\n\nfor return 4 exchange policy details,\nVisit: www. imtiaz.com. ok/return-policies\n     convert it to json and just give me json not any single other message?", stream=False)
    # message = resp['message']
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

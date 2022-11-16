import cherrypy

class CherryPyApp(object):
    @cherrypy.expose
    def hello(self):
        return {"message": "hello world"}
    
    @cherrypy.expose
    def user(self, username):
        return {"user": username}
    
    @cherrypy.expose
    def exclude(self, param):
        return {"message": param}
    
    @cherrypy.expose
    def healthzz(self):
        return {"message": "ok"}

def make_app():
    cherrypy.quickstart(CherryPyApp())
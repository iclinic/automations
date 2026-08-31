# South (Django < 1.7). Traz `class Migration`, casando o marcador do Django,
# mas nao tem o atributo `operations` que o parser le: o portao de parser
# silencioso e o unico que impede um `delete_column` de sair como "nada a
# reportar".
from south.db import db
from south.v2 import SchemaMigration


class Migration(SchemaMigration):

    def forwards(self, orm):
        db.delete_column("subjects_subject", "cpf")

    def backwards(self, orm):
        db.add_column("subjects_subject", "cpf", self.gf("CharField")(max_length=14))

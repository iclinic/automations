<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * A migração escrita com o **schema builder** do Doctrine em vez de `addSql()`.
 * Este parser só lê `addSql`, então devolve lista vazia — e lista vazia se lê,
 * a jusante, como "nada a reportar". O portão do parser mudo em
 * `detect/__init__.py` é quem transforma isso em `unknown`: o parser certo foi
 * escolhido, voltou de mãos vazias, e o arquivo declara operações.
 *
 * É o mesmo caso do South no dispatch do Django, agora no stack do Doctrine.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120800 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Cria a tabela de posicoes pelo schema builder';
    }

    public function up(Schema $schema): void
    {
        $table = $schema->createTable('warehouse_bin');
        $table->addColumn('bin_label', 'string', ['length' => 40]);
        $table->setPrimaryKey(['bin_label']);
    }

    public function down(Schema $schema): void
    {
        $schema->dropTable('warehouse_bin');
    }
}

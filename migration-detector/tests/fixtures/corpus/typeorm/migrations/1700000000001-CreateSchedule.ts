import {MigrationInterface, QueryRunner} from "typeorm";

export class CreateSchedule1700000000001 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`CREATE TABLE "schedule" ("id" SERIAL NOT NULL, "room" character varying NOT NULL, CONSTRAINT "PK_schedule" PRIMARY KEY ("id"))`);
        await queryRunner.query(`CREATE INDEX "IDX_schedule_room" ON "schedule" ("room")`);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
